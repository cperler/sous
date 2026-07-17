"""Deterministic TEST + DELIVER ENGINE-lane runners (#33): $0 shell test-run and PR-open,
no model call. Unit-tests run real (fast) shell commands for TEST and an injected runner
for DELIVER, so nothing touches the network or a real gh. Structured outputs are validated
against the canonical stage JSON schemas."""

from __future__ import annotations

from collections import namedtuple

from jsonschema import Draft202012Validator

from adapters.execution.deterministic_deliver import DeterministicDeliverRunner
from adapters.execution.deterministic_setup import DeterministicSetupRunner
from adapters.execution.deterministic_test import DeterministicTestRunner
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.failure_classifier import Failure
from orchestrator.schemas.enums import (
    ExecutionLane,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
)
from orchestrator.schemas.stage_schemas import load_stage_schema
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result

_ENGINE = LanePolicy(execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE, allow_fallback=False)


def _assert_schema_valid(ref: str, out: dict) -> None:
    Draft202012Validator(load_stage_schema(ref)).validate(out)


def _wi(stage: Stage, *, schema_ref: str, cwd=None, context=None, checkpoint_tag=None) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="#42", stage=stage, prompt="p",
        schema_ref=schema_ref, model="engine", lane_policy=_ENGINE, created_at="now",
        cwd=cwd, context=context, checkpoint_tag=checkpoint_tag,
    )


# --- TEST runner ----------------------------------------------------------
class _Classifier:
    """Parses 'FAILED <id>' lines into unit failures (test-controllable)."""

    def classify(self, test_output: str) -> list[Failure]:
        return [
            Failure(test=line.split(" ", 1)[1], kind="unit")
            for line in test_output.splitlines()
            if line.startswith("FAILED ")
        ]

    def impacted_tests(self, changed_files):
        return []


class _TestProj:
    def __init__(self, *, unit, e2e=None, shell=None, classifier=None):
        self._unit, self._e2e, self._shell = unit, e2e, shell
        self.classifier = classifier if classifier is not None else _Classifier()

    def test_unit_cmd(self, files=None):
        return self._unit

    def test_e2e_cmd(self, files=None):
        return self._e2e

    def test_shell_cmd(self, files=None):
        return self._shell


def test_test_green_run_succeeds_schema_valid(tmp_path) -> None:
    res = DeterministicTestRunner(_TestProj(unit=["sh", "-c", "exit 0"])).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={"baseline_failures": []})
    )
    assert res.status is ResultStatus.SUCCESS
    assert (res.lane_used.execution_mode, res.lane_used.provider) == (ExecutionMode.ENGINE, Provider.NONE)
    out = res.structured_output
    assert out["passed"] is True and out["failures"] == []
    assert out["tests_meaningful"] is True  # never false from a script — REVIEW judges it
    _assert_schema_valid("test", out)


def test_test_caused_failure_fails_with_classified_kind(tmp_path) -> None:
    proj = _TestProj(unit=["sh", "-c", "echo 'FAILED tests/test_x.py::t1'; exit 1"])
    res = DeterministicTestRunner(proj).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={"baseline_failures": []})
    )
    assert res.status is ResultStatus.FAILURE  # nonzero exit → engine's retry/fix loop
    out = res.structured_output
    assert out["passed"] is False and out["failures"] == ["tests/test_x.py::t1"]
    assert "unit" in (res.error or "")  # classified failure kind surfaced
    _assert_schema_valid("test", out)


def test_test_unparsed_failure_keeps_bounded_output_tail(tmp_path) -> None:
    # classifier parses nothing → honest caused red with a BOUNDED output tail in the notes.
    proj = _TestProj(
        unit=["sh", "-c", "python3 -c \"print('A'*6000 + 'ZZTAILMARK')\"; exit 2"],
        classifier=_Classifier(),  # no 'FAILED ' lines → no parsed failures
    )
    res = DeterministicTestRunner(proj).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={})
    )
    assert res.status is ResultStatus.FAILURE
    out = res.structured_output
    assert out["failures"] == ["<unit rc=2>"] and "unknown" in (res.error or "")
    assert "ZZTAILMARK" in out["validation_notes"]  # the TAIL survives
    assert "A" * 4001 not in out["validation_notes"]  # ... but the head is truncated (bounded)
    _assert_schema_valid("test", out)


def test_test_inherited_baseline_failure_is_not_counted(tmp_path) -> None:
    proj = _TestProj(unit=["sh", "-c", "echo 'FAILED a::t1'; exit 1"])
    res = DeterministicTestRunner(proj).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path),
            context={"baseline_failures": ["a::t1"]})
    )
    assert res.status is ResultStatus.SUCCESS  # RED at base → inherited, not this change's fault
    out = res.structured_output
    assert out["passed"] is True and out["failures"] == []
    assert "inherited" in out["validation_notes"]


def test_test_splits_inherited_from_caused(tmp_path) -> None:
    proj = _TestProj(unit=["sh", "-c", "printf 'FAILED a::t1\\nFAILED b::t2\\n'; exit 1"])
    res = DeterministicTestRunner(proj).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path),
            context={"baseline_failures": ["a::t1"]})
    )
    assert res.status is ResultStatus.FAILURE
    out = res.structured_output
    assert out["failures"] == ["b::t2"]  # only the NEW failure counts
    assert "inherited" in out["validation_notes"]


def test_test_skips_true_sentinel_e2e_and_shell(tmp_path) -> None:
    proj = _TestProj(unit=["sh", "-c", "exit 0"], e2e=["true"], shell=["true"])
    res = DeterministicTestRunner(proj).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={})
    )
    assert res.status is ResultStatus.SUCCESS
    notes = res.structured_output["validation_notes"]
    assert "e2e" not in notes and "shell" not in notes  # sentinel → not run


def test_test_runs_e2e_when_defined_and_reports_its_failure(tmp_path) -> None:
    proj = _TestProj(
        unit=["sh", "-c", "exit 0"],
        e2e=["sh", "-c", "echo 'FAILED e2e::t'; exit 1"],
    )
    res = DeterministicTestRunner(proj).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={})
    )
    assert res.status is ResultStatus.FAILURE
    assert res.structured_output["failures"] == ["e2e::t"]


def test_test_timeout_fails_as_infra(tmp_path) -> None:
    proj = _TestProj(unit=["sh", "-c", "sleep 3"])
    res = DeterministicTestRunner(proj, timeout_s=0.2).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={})
    )
    assert res.status is ResultStatus.FAILURE
    assert "infra" in (res.error or "") and "<unit timeout>" in res.structured_output["failures"]


def test_test_no_commands_passes_vacuously(tmp_path) -> None:
    proj = _TestProj(unit=["true"])  # the no-op sentinel: nothing to run
    res = DeterministicTestRunner(proj).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={})
    )
    assert res.status is ResultStatus.SUCCESS
    out = res.structured_output
    assert out["passed"] is True and "no test commands" in out["validation_notes"]
    _assert_schema_valid("test", out)


# --- DELIVER runner (injected git/gh) -------------------------------------
_CP = namedtuple("CP", ["returncode", "stdout", "stderr"])


def _cp(rc: int = 0, out: str = "", err: str = "") -> _CP:
    return _CP(rc, out, err)


class _FakeGh:
    """Records (argv, cwd) and answers by argv shape — no real git/gh/network."""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv, cwd=None, **_kw):
        self.calls.append((list(argv), cwd))
        return self.responder(list(argv))

    def ran(self, *needles: str) -> bool:
        return any(all(n in " ".join(argv) for n in needles) for argv, _ in self.calls)


def _deliver_wi(cwd="/wt/42", context=None) -> WorkItem:
    ctx = {"task_id": "#42", "title": "Fix the bug", "issue_number": 42}
    if context is not None:
        ctx = {**ctx, **context}
    return _wi(Stage.DELIVER, schema_ref="deliver", cwd=cwd, context=ctx)


def test_deliver_opens_fresh_pr_with_closes(tmp_path) -> None:
    def responder(argv):
        if argv[:2] == ["git", "rev-parse"]:
            return _cp(0, "task/42\n")
        if "rev-list" in argv:
            return _cp(0, "2\n")
        if "push" in argv:
            return _cp(0)
        if argv[:3] == ["gh", "pr", "list"]:
            return _cp(0, "")  # no existing PR
        if argv[:3] == ["gh", "pr", "create"]:
            return _cp(0, "https://github.com/o/r/pull/77\n")
        return _cp(0)

    gh = _FakeGh(responder)
    res = DeterministicDeliverRunner(FakeProject(), runner=gh).dispatch(_deliver_wi())

    assert res.status is ResultStatus.SUCCESS
    out = res.structured_output
    assert out["pr_url"].endswith("/pull/77") and out["pr_number"] == 77
    _assert_schema_valid("deliver", out)
    # the create call carries Closes #N + the task id/title, and runs in the worktree.
    create = next(c for c in gh.calls if c[0][:3] == ["gh", "pr", "create"])
    argv, cwd = create
    body = argv[argv.index("--body") + 1]
    title = argv[argv.index("--title") + 1]
    assert "Closes #42" in body and "#42" in title and cwd == "/wt/42"


def test_deliver_fix_cycle_reuses_pr_no_duplicate(tmp_path) -> None:
    def responder(argv):
        if argv[:2] == ["git", "rev-parse"]:
            return _cp(0, "task/42\n")
        if "rev-list" in argv:
            return _cp(0, "1\n")
        if "push" in argv:
            return _cp(0)
        return _cp(0)

    gh = _FakeGh(responder)
    res = DeterministicDeliverRunner(FakeProject(), runner=gh).dispatch(
        _deliver_wi(context={
            "pr_url": "https://github.com/o/r/pull/77", "pr_number": 77, "review_cycles": 2,
        })
    )

    assert res.status is ResultStatus.SUCCESS
    out = res.structured_output
    assert out["pr_url"].endswith("/pull/77") and out["pr_number"] == 77 and out.get("reused") is True
    assert gh.ran("push")  # branch is re-pushed so the existing PR reflects the fix
    assert not gh.ran("gh", "pr", "create")  # NEVER a duplicate PR
    # #68: the reuse path leaves an advisory comment on the PR (the optional half of #33).
    comment = next(c for c in gh.calls if c[0][:3] == ["gh", "pr", "comment"])
    argv, _cwd = comment
    assert "https://github.com/o/r/pull/77" in argv  # selected by the reused PR url
    body = argv[argv.index("--body") + 1]
    # A genuine review cycle: names the branch, the review-fix commits, and the cycle number.
    assert "task/42" in body and "review-fix commits" in body and "fix cycle 2" in body


def test_deliver_reuse_comment_uses_number_when_no_url(tmp_path) -> None:
    # No folded pr_url: reuse is discovered via `gh pr list`, and the advisory comment then
    # selects the PR by number (a url isn't available from the list shape here).
    def responder(argv):
        if argv[:2] == ["git", "rev-parse"]:
            return _cp(0, "task/42\n")
        if "rev-list" in argv:
            return _cp(0, "1\n")
        if "push" in argv:
            return _cp(0)
        if argv[:3] == ["gh", "pr", "list"]:
            return _cp(0, '[{"number": 88}]')  # existing PR, number only
        return _cp(0)

    gh = _FakeGh(responder)
    res = DeterministicDeliverRunner(FakeProject(), runner=gh).dispatch(_deliver_wi())
    assert res.status is ResultStatus.SUCCESS and res.structured_output.get("reused") is True
    comment = next(c for c in gh.calls if c[0][:3] == ["gh", "pr", "comment"])
    assert "88" in comment[0]  # PR selected by number
    body = comment[0][comment[0].index("--body") + 1]
    # #118: no review_cycles in context (a raw re-run) → generic wording, never "review-fix
    # commits" or a cycle number the run didn't have.
    assert "fix cycle" not in body and "review-fix commits" not in body
    assert "updated commits" in body


def test_deliver_reuse_comment_failure_never_fails_stage(tmp_path) -> None:
    # gh pr comment blowing up (or gh missing) must NOT fail an already-delivered reuse.
    def responder(argv):
        if argv[:2] == ["git", "rev-parse"]:
            return _cp(0, "task/42\n")
        if "rev-list" in argv:
            return _cp(0, "1\n")
        if "push" in argv:
            return _cp(0)
        if argv[:3] == ["gh", "pr", "comment"]:
            raise RuntimeError("gh comment exploded")
        return _cp(0)

    res = DeterministicDeliverRunner(FakeProject(), runner=_FakeGh(responder)).dispatch(
        _deliver_wi(context={"pr_url": "https://github.com/o/r/pull/77", "pr_number": 77})
    )
    assert res.status is ResultStatus.SUCCESS  # deliver succeeded despite the comment error
    assert res.structured_output["pr_number"] == 77 and res.structured_output.get("reused") is True


def test_deliver_refuses_when_no_commits_and_no_existing_pr(tmp_path) -> None:
    # The genuine empty-PR case: zero commits vs base AND no PR already open for the head.
    def responder(argv):
        if argv[:2] == ["git", "rev-parse"]:
            return _cp(0, "task/42\n")
        if "rev-list" in argv:
            return _cp(0, "0\n")  # zero commits vs base
        if argv[:3] == ["gh", "pr", "list"]:
            return _cp(0, "")  # ...and no existing PR
        return _cp(0)

    gh = _FakeGh(responder)
    res = DeterministicDeliverRunner(FakeProject(), runner=gh).dispatch(_deliver_wi())

    assert res.status is ResultStatus.FAILURE
    assert "no commits" in (res.error or "")
    assert not gh.ran("push") and not gh.ran("gh", "pr", "create")  # no empty PR


def test_deliver_no_commits_reuses_existing_pr_as_noop_success(tmp_path) -> None:
    # #168: a fix-cycle DELIVER on a branch with NO new commits vs base but an EXISTING open
    # PR (same head) is a no-op reuse SUCCESS, not the empty-PR breaker — the PR IS the
    # deliverable. Nothing changed, so DO NOT re-push or leave a re-pushed advisory comment.
    def responder(argv):
        if argv[:2] == ["git", "rev-parse"]:
            return _cp(0, "task/42\n")
        if "rev-list" in argv:
            return _cp(0, "0\n")  # zero commits vs base
        if argv[:3] == ["gh", "pr", "list"]:
            return _cp(0, '[{"number": 156, "url": "https://github.com/o/r/pull/156"}]')
        return _cp(0)

    gh = _FakeGh(responder)
    res = DeterministicDeliverRunner(FakeProject(), runner=gh).dispatch(_deliver_wi())

    assert res.status is ResultStatus.SUCCESS
    out = res.structured_output
    assert out["pr_number"] == 156 and out["pr_url"].endswith("/pull/156") and out["reused"] is True
    _assert_schema_valid("deliver", out)
    # no-op: never push, never open a duplicate, never leave a "re-pushed" comment.
    assert not gh.ran("push")
    assert not gh.ran("gh", "pr", "create")
    assert not gh.ran("gh", "pr", "comment")


def test_deliver_no_commits_reuses_folded_pr_url_without_gh_list(tmp_path) -> None:
    # The folded pr_url (engine sets it once DELIVER has run) short-circuits reuse on the
    # no-commit path too — no gh pr list needed, still a no-op reuse success.
    def responder(argv):
        if argv[:2] == ["git", "rev-parse"]:
            return _cp(0, "task/42\n")
        if "rev-list" in argv:
            return _cp(0, "0\n")
        return _cp(0)

    gh = _FakeGh(responder)
    res = DeterministicDeliverRunner(FakeProject(), runner=gh).dispatch(
        _deliver_wi(context={"pr_url": "https://github.com/o/r/pull/156", "pr_number": 156})
    )
    assert res.status is ResultStatus.SUCCESS
    out = res.structured_output
    assert out["pr_number"] == 156 and out.get("reused") is True
    assert not gh.ran("push") and not gh.ran("gh", "pr", "list")
    assert not gh.ran("gh", "pr", "comment") and not gh.ran("gh", "pr", "create")


def test_deliver_fails_on_push_error(tmp_path) -> None:
    def responder(argv):
        if argv[:2] == ["git", "rev-parse"]:
            return _cp(0, "task/42\n")
        if "rev-list" in argv:
            return _cp(0, "2\n")
        if "push" in argv:
            return _cp(1, "", "remote rejected")
        return _cp(0)

    res = DeterministicDeliverRunner(FakeProject(), runner=_FakeGh(responder)).dispatch(_deliver_wi())
    assert res.status is ResultStatus.FAILURE and "push failed" in (res.error or "")


def test_deliver_dispatch_never_escapes_a_raising_runner(tmp_path) -> None:
    def boom(argv, cwd=None, **_kw):
        raise RuntimeError("gh exploded")

    res = DeterministicDeliverRunner(FakeProject(), runner=boom).dispatch(_deliver_wi())
    assert res.status is ResultStatus.FAILURE and "exploded" in (res.error or "")


def test_deliver_requires_worktree_cwd(tmp_path) -> None:
    res = DeterministicDeliverRunner(FakeProject(), runner=_FakeGh(lambda a: _cp(0))).dispatch(
        _deliver_wi(cwd=None)
    )
    assert res.status is ResultStatus.FAILURE and "worktree" in (res.error or "")


# --- wiring: the ENGINE cell serves all three, engine routes opt-ins there ----
def test_engine_runner_delegates_test_and_deliver(tmp_path) -> None:
    # The single (ENGINE, NONE) runner dispatches TEST/DELIVER by stage (no separate cell).
    setup = DeterministicSetupRunner(_TestProj(unit=["sh", "-c", "exit 0"]))
    test_res = setup.dispatch(_wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path),
                                  context={"baseline_failures": []}))
    assert test_res.status is ResultStatus.SUCCESS and test_res.structured_output["passed"] is True
    # DELIVER with no cwd fails BEFORE any subprocess — proves delegation reaches the deliver
    # runner without touching git/gh.
    deliver_res = setup.dispatch(_wi(Stage.DELIVER, schema_ref="deliver", cwd=None, context={}))
    assert deliver_res.status is ResultStatus.FAILURE and "worktree" in (deliver_res.error or "")


def test_engine_routes_opted_in_stages_to_engine_lane(tmp_path) -> None:
    eng = Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), FakeProject())
    eng.create_run("r")
    task = eng.add_task("r", "t1", deterministic_stages=[Stage.TEST, Stage.DELIVER])
    assert task.deterministic_stages == (Stage.TEST, Stage.DELIVER)  # persisted

    lanes: dict[Stage, ExecutionMode] = {}
    ctx_seen: dict[Stage, dict | None] = {}
    while (w := eng.next_work("r", "t1")) is not None:
        lanes[w.stage] = w.lane_policy.execution_mode
        ctx_seen[w.stage] = w.context
        eng.record("r", make_result(w))

    # intake is always deterministic; the opted-in TEST/DELIVER now route to the ENGINE lane.
    assert lanes[Stage.INTAKE] is ExecutionMode.ENGINE
    assert lanes[Stage.TEST] is ExecutionMode.ENGINE
    assert lanes[Stage.DELIVER] is ExecutionMode.ENGINE
    # the un-opted model stages are untouched (default interactive lane).
    assert lanes[Stage.SCOPE] is ExecutionMode.INTERACTIVE
    assert lanes[Stage.IMPLEMENT] is ExecutionMode.INTERACTIVE
    assert lanes[Stage.REVIEW] is ExecutionMode.INTERACTIVE
    # deterministic stages carry structured context; model stages do not.
    assert ctx_seen[Stage.DELIVER] is not None and ctx_seen[Stage.DELIVER]["task_id"] == "t1"
    assert ctx_seen[Stage.SCOPE] is None


# --- #68: micro/lite presets default to deterministic TEST/DELIVER --------------
def _eng(tmp_path) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), FakeProject())


def test_lite_preset_defaults_to_deterministic_test_and_deliver(tmp_path) -> None:
    eng = _eng(tmp_path)
    eng.create_run("r", ExecutionLane.LITE)
    task = eng.add_task("r", "t1")  # no explicit deterministic_stages
    assert task.deterministic_stages == (Stage.TEST, Stage.DELIVER)


def test_micro_preset_defaults_to_deterministic_deliver_only(tmp_path) -> None:
    # MICRO has no TEST stage, so the default is DELIVER only (intersected with the pipeline).
    eng = _eng(tmp_path)
    eng.create_run("r", ExecutionLane.MICRO)
    task = eng.add_task("r", "t1")
    assert task.deterministic_stages == (Stage.DELIVER,)
    assert Stage.TEST not in task.pipeline


def test_full_preset_keeps_model_test_and_deliver(tmp_path) -> None:
    eng = _eng(tmp_path)
    eng.create_run("r", ExecutionLane.FULL)
    task = eng.add_task("r", "t1")
    assert task.deterministic_stages == ()  # FULL pays for model TEST/DELIVER


def test_explicit_deterministic_stages_override_the_preset_default(tmp_path) -> None:
    eng = _eng(tmp_path)
    eng.create_run("r", ExecutionLane.LITE)
    task = eng.add_task("r", "t1", deterministic_stages=[Stage.DELIVER])
    assert task.deterministic_stages == (Stage.DELIVER,)  # caller wins, TEST stays model


def test_explicit_pipeline_pin_opts_out_of_the_preset_default(tmp_path) -> None:
    # A bespoke pipeline states its own deterministic stages; the lane preset default
    # does not silently reach it.
    eng = _eng(tmp_path)
    eng.create_run("r", ExecutionLane.LITE)
    task = eng.add_task("r", "t1", pipeline=[Stage.IMPLEMENT, Stage.TEST, Stage.DELIVER])
    assert task.deterministic_stages == ()
