"""#389: DELIVER pushes, PUBLISH opens the PR — and PUBLISH runs AFTER REVIEW.

Before this split every lane ran ``…, deliver, review``, so a pull request was opened
against trunk before anything had judged the work and a rejected task re-delivered onto
that open PR on every fix cycle. Two live failures motivated the change: #378 (a fix cycle
re-delivered onto a ``pr_url`` whose PR had since been merged, so the approved fix was
pushed with no PR open) and ``batch-380-381`` task #381 (three rejections, so PR #387 sat
open through three rounds of rejected work).

These tests pin the properties the reordering is supposed to buy — and the one it most
risks, which is that a run dying mid-review must still leave its work on the remote.
"""

from __future__ import annotations

import json

import pytest

from adapters.execution.deterministic_publish import DeterministicPublishRunner
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import _CHILD_PIPELINES, Engine
from orchestrator.schemas.enums import (
    LANE_STAGES,
    ExecutionMode,
    Provider,
    QualityTier,
    ResultStatus,
    Stage,
    StageStatus,
    TaskState,
)
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.stages import STAGE_SPECS
from orchestrator.state_machine import CONTEXT_KEYS
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

REJECTION = {
    "approved": False,
    "issues": [
        {"severity": "critical", "file": "a.py", "line": 12,
         "description": "breaks the invariant", "suggested_fix": "guard the None case"},
    ],
}


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "costs.jsonl"), project, **kw)


def _drive_to_review(eng, run="r1", task="t1", *, seen: list | None = None):
    """Drive the pipeline green up to (and returning) the REVIEW WorkItem.

    ``seen`` collects the stages driven along the way, so a caller can count how many
    times a stage ran across the WHOLE task rather than only after a rejection."""
    while (w := eng.next_work(run, task)).stage is not Stage.REVIEW:
        if seen is not None:
            seen.append(w.stage)
        eng.record(run, make_result(w))
    return w


# --- 1. pipeline shape: every lane and every tier ends in PUBLISH -----------------

@pytest.mark.parametrize("lane", sorted(LANE_STAGES, key=lambda x: x.value))
def test_every_lane_delivers_before_review_and_publishes_last(lane) -> None:
    """MICRO has no TEST and LITE has no SCOPE — the ordering must hold for all of them."""
    stages = LANE_STAGES[lane]
    assert stages[-1] is Stage.PUBLISH, f"{lane.value} does not end in PUBLISH"
    assert stages.index(Stage.DELIVER) < stages.index(Stage.REVIEW) < stages.index(Stage.PUBLISH)


@pytest.mark.parametrize("tier", sorted(_CHILD_PIPELINES, key=lambda x: x.value))
def test_every_quality_tier_publishes_last(tier) -> None:
    """A decomposed child's pipeline gets the same ordering.

    Tier ``none`` has no REVIEW at all, so PUBLISH simply follows DELIVER there — proof
    that the approval gate is PUBLISH's POSITION, never a condition it evaluates."""
    stages = _CHILD_PIPELINES[tier]
    assert stages[-1] is Stage.PUBLISH
    assert stages.index(Stage.DELIVER) < stages.index(Stage.PUBLISH)
    if tier is QualityTier.NONE:
        assert Stage.REVIEW not in stages
    else:
        assert stages.index(Stage.REVIEW) < stages.index(Stage.PUBLISH)


def test_publish_is_deterministic_and_makes_no_commits() -> None:
    """So it never costs a model call, and never enters the commit-attribution audit.

    ``tests/test_commit_attribution.py`` derives the COMMITTING stage set as
    ``checkpoint and not deterministic``; PUBLISH fails both halves. Asserted explicitly
    rather than left to that derivation, because it is a property of PUBLISH, not a
    coincidence of how another test computes a set."""
    spec = STAGE_SPECS[Stage.PUBLISH]
    assert spec.deterministic is True
    assert spec.checkpoint is False
    assert spec.effort is None  # the ENGINE lane has no model to throttle


# --- 2. the headline behavior: no PR until the work is approved -------------------

def test_rejected_then_fixed_task_pushes_twice_but_publishes_once(tmp_path, project) -> None:
    """The #381 shape: three rejections used to mean four PRs' worth of churn.

    DELIVER runs once per cycle (the branch is re-pushed, which is what keeps the work
    recoverable), while PUBLISH runs exactly once, at the end, after the approval."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    stages_run: list[Stage] = []
    review = _drive_to_review(eng, seen=stages_run)
    out = eng.record("r1", make_result(review, structured_output=REJECTION))
    assert out["outcome"] == "review_rejected_fix_cycle"
    # Mid-cycle, the task holds NO pr_url — there is nothing for a later cycle to reuse,
    # go stale, or re-deliver onto. This is the #378 class closed by construction.
    assert eng.store.load_task("r1", "t1").pr_url is None

    stages_run.append(review.stage)
    while (w := eng.next_work("r1", "t1")) is not None:
        stages_run.append(w.stage)
        payload = {"approved": True, "issues": []} if w.stage is Stage.REVIEW else None
        eng.record("r1", make_result(w, structured_output=payload))

    assert stages_run.count(Stage.DELIVER) == 2  # re-pushed on the fix cycle
    assert stages_run.count(Stage.PUBLISH) == 1  # published once, after approval
    assert stages_run[-1] is Stage.PUBLISH
    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.COMPLETED
    assert task.pr_url and task.pr_url.endswith("/1234")


def test_a_task_that_exhausts_its_fix_budget_never_opens_a_pr(tmp_path, project) -> None:
    """A parked, never-approved task must leave no PR behind at all."""
    eng = _engine(tmp_path, project, max_review_cycles=0)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    out = eng.record("r1", make_result(_drive_to_review(eng), structured_output=REJECTION))

    assert out["outcome"] == "review_rejected_held"
    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.BLOCKED_ON_HUMAN
    assert task.pr_url is None and task.pr_number is None
    assert task.stages[Stage.PUBLISH].status is StageStatus.PENDING  # never dispatched


def test_review_is_not_given_a_pr_url_to_read(tmp_path, project) -> None:
    """What REVIEW reads instead: the ``base_sha..HEAD`` range folded at INTAKE."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    # INTAKE reports the base the task forked from; the reviewer's range starts there.
    intake = eng.next_work("r1", "t1")
    eng.record("r1", make_result(intake, structured_output={
        "branch": "task/t1", "worktree": "/wt/t1", "baseline_captured": True,
        "base_sha": "f" * 40,
    }))
    review = _drive_to_review(eng)

    # base_sha is an INTAKE-folded context key, so it reaches REVIEW's prompt; pr_url is
    # not folded until PUBLISH, which has not run.
    assert "base_sha" in CONTEXT_KEYS[Stage.INTAKE]
    assert CONTEXT_KEYS[Stage.PUBLISH] == ("pr_number", "pr_url")
    task_context = eng.store.load_task("r1", "t1").context
    assert "pr_url" not in task_context and "pr_number" not in task_context
    assert task_context["base_sha"] == "f" * 40
    # The model REVIEW reads its PROMPT (only deterministic stages get a structural
    # context), so the range has to be legible there.
    assert "f" * 40 in review.prompt
    assert "base_sha..HEAD" in review.prompt and "pr_url" not in review.prompt


# --- 3. the property the reordering most risks: a dead run keeps its work ---------

def test_a_run_that_dies_after_deliver_still_has_its_work_on_the_remote(
    tmp_path, project
) -> None:
    """#385's class must not get worse.

    Moving the PR behind REVIEW widens the window in which a run can die with work in
    flight, so the durability property has to come from the PUSH, not from the PR. This
    kills the run at the most dangerous point — implemented, delivered, mid-review — and
    asserts the branch and its head sha are already recorded as remote.
    """
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    while (w := eng.next_work("r1", "t1")).stage is not Stage.REVIEW:
        eng.record("r1", make_result(w))
    # …and now the run dies: REVIEW is dispatched and never recorded.

    task = eng.store.load_task("r1", "t1")
    assert task.stages[Stage.DELIVER].status is StageStatus.COMPLETED
    delivered = task.stages[Stage.DELIVER].output or {}
    assert delivered["branch"]  # the branch the push landed on
    assert len(delivered["pushed_head_sha"]) == 40  # a real head, verified against origin
    # The task never reached PUBLISH, so there is no PR — but the work is NOT lost.
    assert task.pr_url is None
    assert task.stages[Stage.PUBLISH].status is StageStatus.PENDING


def test_deliver_that_reports_no_pushed_head_is_vetoed(tmp_path, project) -> None:
    """The gate that makes the durability claim checkable rather than asserted.

    Once DELIVER stops opening a PR, ``pr_not_opened`` no longer covers it; without a
    replacement a DELIVER could report SUCCESS having pushed nothing and the run would
    reach PUBLISH with an unpublished branch."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")).stage is not Stage.DELIVER:
        eng.record("r1", make_result(w))

    eng.record("r1", make_result(w, structured_output={"branch": "task/t1",
                                                       "pushed_head_sha": "abc123"}))

    task = eng.store.load_task("r1", "t1")
    assert task.stages[Stage.DELIVER].status is StageStatus.FAILED
    assert "pushed_head_sha is not a full commit sha" in (task.last_error or "")


# --- 4. the publish runner's own idempotency --------------------------------------

_CP = __import__("collections").namedtuple("CP", ["returncode", "stdout", "stderr"])
_ENGINE_LANE = LanePolicy(
    execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE, allow_fallback=False
)


def _publish_wi(cwd="/wt/42") -> WorkItem:
    return WorkItem.create(
        id="w-publish", run_id="r1", task_id="#42", stage=Stage.PUBLISH,
        prompt="p", schema_ref="publish", model="engine", lane_policy=_ENGINE_LANE,
        created_at="now", cwd=cwd,
        context={"task_id": "#42", "title": "Fix the bug", "issue_number": 42},
    )


def _pr_json(number: int, *, head: str = "task/42") -> str:
    return (
        f'[{{"number":{number},"url":"https://github.com/o/r/pull/{number}",'
        f'"state":"OPEN","headRefName":"{head}","baseRefName":"main"}}]'
    )


def test_publish_opens_a_pr_that_closes_the_issue(tmp_path) -> None:
    from tests.conftest import FakeProject

    calls: list[list[str]] = []

    def responder(argv, cwd=None, **_kw):
        calls.append(list(argv))
        if argv[:2] == ["git", "rev-parse"]:
            return _CP(0, "task/42\n", "")
        if "rev-list" in argv:
            return _CP(0, "2\n", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return _CP(0, "[]", "")
        if argv[:3] == ["gh", "pr", "create"]:
            return _CP(0, "https://github.com/o/r/pull/77\n", "")
        return _CP(0, "", "")

    res = DeterministicPublishRunner(FakeProject(), runner=responder).dispatch(_publish_wi())

    assert res.status is ResultStatus.SUCCESS
    assert res.structured_output == {"pr_number": 77,
                                     "pr_url": "https://github.com/o/r/pull/77"}
    create = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert "Closes #42" in create[create.index("--body") + 1]


def test_publish_retried_after_a_partial_failure_opens_no_duplicate(tmp_path) -> None:
    """PUBLISH has its own retry budget, so an attempt that created the PR and then died
    before its result was recorded WILL be retried. The duplicate guard survives the split
    for exactly this reason — not for #378's staleness, which no longer exists."""
    from tests.conftest import FakeProject

    creates = 0

    def responder(argv, cwd=None, **_kw):
        nonlocal creates
        if argv[:2] == ["git", "rev-parse"]:
            return _CP(0, "task/42\n", "")
        if "rev-list" in argv:
            return _CP(0, "2\n", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return _CP(0, _pr_json(77), "")  # the first attempt's PR is already open
        if argv[:3] == ["gh", "pr", "create"]:
            creates += 1
            return _CP(0, "https://github.com/o/r/pull/78\n", "")
        return _CP(0, "", "")

    res = DeterministicPublishRunner(FakeProject(), runner=responder).dispatch(_publish_wi())

    assert res.status is ResultStatus.SUCCESS
    assert creates == 0  # it reused, it did not create
    assert res.structured_output["pr_number"] == 77
    assert res.structured_output["reused"] is True


def test_publish_refuses_a_branch_with_no_commits(tmp_path) -> None:
    from tests.conftest import FakeProject

    def responder(argv, cwd=None, **_kw):
        if argv[:2] == ["git", "rev-parse"]:
            return _CP(0, "task/42\n", "")
        if "rev-list" in argv:
            return _CP(0, "0\n", "")
        return _CP(0, "", "")

    res = DeterministicPublishRunner(FakeProject(), runner=responder).dispatch(_publish_wi())
    assert res.status is ResultStatus.FAILURE and "no commits" in (res.error or "")


def test_publish_treats_an_unanswerable_pr_lookup_as_an_error_not_an_absence(tmp_path) -> None:
    """Treating "I could not ask GitHub" as "there is no PR" is how a duplicate gets born."""
    from tests.conftest import FakeProject

    def responder(argv, cwd=None, **_kw):
        if argv[:2] == ["git", "rev-parse"]:
            return _CP(0, "task/42\n", "")
        if "rev-list" in argv:
            return _CP(0, "2\n", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return _CP(1, "", "API rate limit exceeded")
        raise AssertionError("must not reach gh pr create")

    res = DeterministicPublishRunner(FakeProject(), runner=responder).dispatch(_publish_wi())
    assert res.status is ResultStatus.FAILURE and "could not look up" in (res.error or "")


# --- 5. completion evidence: an OPEN PR, not a merely non-empty url ---------------

def test_a_publish_that_opened_nothing_is_vetoed_before_completion(tmp_path, project) -> None:
    """First line of defence: the publish gate refuses a PR-less SUCCESS outright."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")).stage is not Stage.PUBLISH:
        eng.record("r1", make_result(w))

    out = eng.record("r1", make_result(w, structured_output={"pr_number": 0, "pr_url": ""}))

    assert out["outcome"] != "task_completed"
    task = eng.store.load_task("r1", "t1")
    assert task.stages[Stage.PUBLISH].status is StageStatus.FAILED
    assert "no pull request was opened" in (task.last_error or "")


def test_completion_audit_flags_a_task_that_completed_without_publishing(
    tmp_path, project
) -> None:
    """Second line: the audit gap #378 identified, now closed.

    ``completed_with_pr`` only ever scrutinised tasks that already HAD a url, so a task
    that ran PUBLISH and completed with NO url at all escaped entirely — the loudest
    possible delivery failure reading as clean. The event now carries whether the task's
    pipeline owed a PR, so the audit can tell that from a branchless non-code task."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))

    # Rewrite the completion receipt as if PUBLISH had produced no url (the shape a
    # partially-successful delivery leaves behind), then re-audit from the events alone.
    path = tmp_path / "events.jsonl"
    lines = []
    for line in path.read_text().splitlines():
        ev = json.loads(line)
        if ev.get("type") == "task_completed":
            ev["pr_url"] = None
        lines.append(json.dumps(ev))
    path.write_text("\n".join(lines) + "\n")

    audit = eng.completion_notes_audit("r1")
    assert audit["delivery_invalid"] == 1
    assert audit["delivery_invalid_by_task"] == {"t1": 1}
    assert "ran PUBLISH but recorded no pr_url" in audit["notes"][0]["error"]


def test_completion_audit_ignores_a_task_that_never_had_a_publish_stage(
    tmp_path, project
) -> None:
    """The false positive the ``publishes`` flag exists to prevent: a task with no PUBLISH
    in its pipeline owes no PR, so completing without one is correct, not invalid."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", pipeline=[Stage.INTAKE, Stage.IMPLEMENT])
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))

    audit = eng.completion_notes_audit("r1")
    assert audit["delivery_invalid"] == 0
    assert audit["clean"] is True


def test_completion_audit_is_clean_for_a_task_with_an_open_pr(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))

    audit = eng.completion_notes_audit("r1")
    assert audit["delivery_invalid"] == 0
    assert audit["clean"] is True
