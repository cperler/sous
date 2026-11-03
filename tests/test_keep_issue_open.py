"""#613 — a task whose PR does not finish its issue keeps that issue open.

Prose did not hold: on ``pred-1141-1291`` and ``pred-1300-1329`` PRs said ``Closes #N`` with
"do not close it on merge" in their own context, and each issue was reopened by hand. So the
setting persists on the Task, both DELIVER lanes write ``Part of #N``, and the engine reads
GitHub's closing references back.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from adapters.execution.deterministic_deliver import DeterministicDeliverRunner, _pr_body
from adapters.github_closing_refs import (
    closing_numbers,
    repo_of_pr_url,
    rewrite_closing_keywords,
)
from adapters.project.base import TaskSpec
from adapters.project.github_issues import GitHubIssuesSource
from orchestrator.batch_plan import BatchPlanError, apply_plan, validate_plan
from orchestrator.cli import main
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import (
    ExecutionLane,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
    TaskState,
)
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.stages import STAGE_SPECS, render_prompt
from orchestrator.state_machine import keep_open_violation
from orchestrator.status_store import StatusStore
from tests.conftest import FakeTaskSource, make_result

PR = "https://github.com/o/r/pull/77"
_ENGINE_LANE = LanePolicy(
    execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE, allow_fallback=False
)


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project)


def _events(eng: Engine, etype: str, run: str = "r1") -> list[dict]:
    return [e for e in eng.store.read_events(run) if e["type"] == etype]


def _labelled(project, labels: list[str], task: str = "t1", *, declared=("experiment",)):
    project.keep_open_labels = declared
    project.task_source.spec_overrides[task] = {"labels": labels}
    return project


# --- where the setting comes from -------------------------------------------------------


@pytest.mark.parametrize(
    ("explicit", "labels", "expected"),
    [
        (None, [], False),
        (None, ["bug"], False),
        (None, ["experiment"], True),
        (None, ["  EXPERIMENT "], True),  # same comparison as the meta-authoring label
        (True, [], True),
        (False, ["experiment"], False),  # a person's choice beats the label
        (True, ["experiment"], True),
    ],
)
def test_add_task_resolves_the_setting(tmp_path, project, explicit, labels, expected) -> None:
    eng = _engine(tmp_path, _labelled(project, labels))
    eng.create_run("r1", ExecutionLane.FULL)
    assert eng.add_task("r1", "t1", keep_issue_open=explicit).keep_issue_open is expected


def test_an_adapter_without_the_attribute_matches_nothing(tmp_path, project) -> None:
    project.task_source.spec_overrides["t1"] = {"labels": ["experiment"]}
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    assert eng.add_task("r1", "t1").keep_issue_open is False
    assert _events(eng, "keep_issue_open_set") == []


def test_a_bare_string_declaration_is_one_label_not_letters(tmp_path, project) -> None:
    eng = _engine(tmp_path, _labelled(project, ["e"], declared="experiment"))
    eng.create_run("r1", ExecutionLane.FULL)
    assert eng.add_task("r1", "t1").keep_issue_open is False


@pytest.mark.parametrize(
    ("explicit", "labels", "source", "label"),
    [(True, [], "explicit", None), (None, ["experiment"], "label", "experiment")],
)
def test_turning_it_on_leaves_a_receipt_naming_why(
    tmp_path, project, explicit, labels, source, label
) -> None:
    eng = _engine(tmp_path, _labelled(project, labels))
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", keep_issue_open=explicit)
    (event,) = _events(eng, "keep_issue_open_set")
    assert (event["source"], event["label"]) == (source, label)


def test_the_setting_survives_a_rebuilt_engine(tmp_path, project) -> None:
    """Every CLI subcommand rebuilds the Engine from defaults (the run-settings rule)."""
    first = _engine(tmp_path, project)
    first.create_run("r1", ExecutionLane.FULL)
    first.add_task("r1", "t1", keep_issue_open=True)
    assert _engine(tmp_path, project).store.load_task("r1", "t1").keep_issue_open is True


def test_refresh_spec_turns_it_on_when_the_label_arrives(tmp_path, project) -> None:
    eng = _engine(tmp_path, _labelled(project, []))
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    project.task_source.spec_overrides["t1"] = {"labels": ["experiment"]}
    eng.refresh_spec("r1", "t1")

    assert eng.store.load_task("r1", "t1").keep_issue_open is True
    (event,) = _events(eng, "keep_issue_open_set")
    assert event["label"] == "experiment"


def test_refresh_spec_never_turns_it_off(tmp_path, project) -> None:
    eng = _engine(tmp_path, _labelled(project, ["experiment"]))
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    project.task_source.spec_overrides["t1"] = {"labels": []}
    eng.refresh_spec("r1", "t1")

    assert eng.store.load_task("r1", "t1").keep_issue_open is True


def test_refresh_spec_does_not_overrule_an_explicit_no(tmp_path, project) -> None:
    eng = _engine(tmp_path, _labelled(project, []))
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", keep_issue_open=False)

    project.task_source.spec_overrides["t1"] = {"labels": ["experiment"]}
    eng.refresh_spec("r1", "t1")

    assert eng.store.load_task("r1", "t1").keep_issue_open is False
    assert _events(eng, "keep_issue_open_set") == []


def test_a_check_only_refresh_changes_nothing(tmp_path, project) -> None:
    eng = _engine(tmp_path, _labelled(project, []))
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    project.task_source.spec_overrides["t1"] = {"labels": ["experiment"]}
    eng.refresh_spec("r1", "t1", check_only=True)

    assert eng.store.load_task("r1", "t1").keep_issue_open is False


class _CreatingSource(FakeTaskSource):
    def __init__(self) -> None:
        super().__init__()
        self.created: dict[str, TaskSpec] = {}

    def create_task(self, title: str, body: str, labels=None) -> str:
        ref = f"child-{len(self.created) + 1}"
        self.created[ref] = TaskSpec(task_id=ref, title=title, body=body, issue_number=900)
        return ref

    def resolve(self, task_id: str) -> TaskSpec:
        return self.created.get(task_id) or super().resolve(task_id)


def test_decomposition_children_do_not_inherit_it(tmp_path, project) -> None:
    """A child is its own new issue, and its PR should close that one. No PR names the
    parent's issue, so the parent's setting has nothing to protect on a child."""
    project._task_source = _CreatingSource()
    project.agent_for = lambda stage, role=None: f"{role}-agent" if role else None
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "parent", pipeline=[Stage.SCOPE, Stage.IMPLEMENT], keep_issue_open=True)

    scope = eng.next_work("r1", "parent")
    output = {"feasible": True, "plan": ["split"], "subtasks": [
        {"id": "a", "description": "first half", "quality_tier": "none"},
        {"id": "b", "description": "second half", "quality_tier": "none"},
    ]}
    assert eng.record("r1", make_result(scope, structured_output=output))["outcome"] == (
        "task_decomposed"
    )

    assert eng.store.load_task("r1", "parent").keep_issue_open is True
    assert eng.store.load_task("r1", "child-1").keep_issue_open is False
    assert eng.store.load_task("r1", "child-2").keep_issue_open is False


# --- the front doors --------------------------------------------------------------------


def _plan(**task) -> dict:
    return {"tasks": [{"task_id": "#1", "pipeline": "full", **task}]}


def test_batch_plan_carries_the_field_to_the_task(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    apply_plan(eng, "r1", _plan(keep_issue_open=True), known_ids=["#1"])
    assert eng.store.load_task("r1", "#1").keep_issue_open is True
    (planned,) = _events(eng, "batch_planned")
    assert planned["tasks"][0]["keep_issue_open"] is True


def test_batch_plan_false_overrides_a_label(tmp_path, project) -> None:
    eng = _engine(tmp_path, _labelled(project, ["experiment"], task="#1"))
    eng.create_run("r1", ExecutionLane.FULL)
    apply_plan(eng, "r1", _plan(keep_issue_open=False), known_ids=["#1"])
    assert eng.store.load_task("r1", "#1").keep_issue_open is False


def test_a_default_task_previews_as_default_not_as_an_explicit_no(tmp_path, project) -> None:
    """Null means "follow the labels", which may well end up on. A preview that said false
    would show the human the opposite of what will run."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    report = apply_plan(eng, "r1", _plan(), known_ids=["#1"], dry_run=True)
    assert report["added"][0]["keep_issue_open"] is None


def test_batch_plan_schema_refuses_a_non_boolean() -> None:
    validate_plan(_plan(keep_issue_open=None), known_ids=["#1"])  # no raise
    with pytest.raises(BatchPlanError, match="keep_issue_open"):
        validate_plan(_plan(keep_issue_open="yes"), known_ids=["#1"])


@pytest.mark.parametrize(
    ("flags", "expected"),
    [([], False), (["--keep-issue-open"], True), (["--no-keep-issue-open"], False)],
)
def test_add_task_cli_flags(tmp_path, capsys, flags, expected) -> None:
    base = ["--root", str(tmp_path), "--run", "run1", "--project", "tests.fakeproject"]
    assert main([*base, "init-run", "--lane", "full"]) == 0
    capsys.readouterr()
    assert main([*base, "add-task", "--task", "#42", *flags]) == 0
    assert json.loads(capsys.readouterr().out)["keep_issue_open"] is expected
    # Read back through a fresh command, so the value is proven persisted.
    assert main([*base, "status"]) == 0
    capsys.readouterr()
    store = StatusStore(tmp_path)
    assert store.load_task("run1", "#42").keep_issue_open is expected


# --- the model lane's prompt ------------------------------------------------------------


def _deliver_prompt(**kw) -> str:
    return render_prompt(Stage.DELIVER, task_id="#42", title="t", body="b", **kw)


def test_prompt_gets_the_right_keyword_when_set() -> None:
    prompt = _deliver_prompt(keep_open_issue=42)
    assert "'Part of #42'" in prompt
    assert "Closes #N" not in prompt
    assert "closing keyword" in prompt


def test_prompt_is_unchanged_without_the_setting() -> None:
    prompt = _deliver_prompt()
    assert "include 'Closes #N' in the PR description so the merge closes the issue. If" in prompt
    assert "Part of" not in prompt
    assert prompt.count("Closes #N") == 1
    assert f"## DELIVER\n{STAGE_SPECS[Stage.DELIVER].template}" in prompt


def test_only_deliver_reads_the_setting() -> None:
    kwargs = {"task_id": "#42", "title": "t", "body": "b"}
    assert render_prompt(Stage.REVIEW, keep_open_issue=42, **kwargs) == render_prompt(
        Stage.REVIEW, **kwargs
    )


def test_engine_passes_the_setting_to_the_deliver_prompt(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", keep_issue_open=True, deterministic_stages=())
    while (work := eng.next_work("r1", "t1")).stage is not Stage.DELIVER:
        assert "Part of #42" not in work.prompt
        eng.record("r1", make_result(work))
    assert "'Part of #42'" in work.prompt
    assert "Closes #N" not in work.prompt


def test_engine_context_tells_the_runner_only_when_set(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    kept = eng.add_task("r1", "kept", keep_issue_open=True)
    plain = eng.add_task("r1", "plain")
    assert eng._deterministic_context(kept)["keep_issue_open"] is True
    assert "keep_issue_open" not in eng._deterministic_context(plain)


# --- recognising closing keywords -------------------------------------------------------


@pytest.mark.parametrize(
    "keyword",
    ["close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved",
     "Closes", "FIXES", "Resolved"],
)
def test_rewrite_covers_every_github_keyword_form(keyword) -> None:
    assert rewrite_closing_keywords(f"{keyword} #12", 12) == "Part of #12"
    assert rewrite_closing_keywords(f"{keyword}: #12", 12) == "Part of #12"


@pytest.mark.parametrize(
    "body",
    [
        "Closes #123",  # the issue's number is only a prefix of this one
        "Closes #1",
        "see #12 for context",
        "prefixes #12",  # contains the letters of a keyword but is not one
        "Do not close it on merge, see #12",
        "Fixes #99",
        "Fixes other/repo#12",  # the same number in another repository is another issue
        "Fixes https://github.com/other/repo/issues/12",
    ],
)
def test_rewrite_leaves_everything_else_alone(body) -> None:
    assert rewrite_closing_keywords(body, 12) == body
    assert rewrite_closing_keywords(body, 12, repo="o/r") == body


def test_qualified_forms_are_rewritten_only_for_the_named_repo() -> None:
    assert rewrite_closing_keywords("Fixes o/r#12", 12) == "Fixes o/r#12"  # repo unknown
    assert rewrite_closing_keywords("Fixes O/R#12", 12, repo="o/r") == "Part of O/R#12"
    assert (
        rewrite_closing_keywords("Closes https://github.com/o/r/issues/12", 12, repo="o/r")
        == "Part of https://github.com/o/r/issues/12"
    )
    assert rewrite_closing_keywords("Closes o/r#123", 12, repo="o/r") == "Closes o/r#123"


def test_rewrite_handles_several_mentions_in_one_body() -> None:
    body = "Closes #12\n\nFixes #99, and resolved #12 again"
    assert rewrite_closing_keywords(body, 12) == "Part of #12\n\nFixes #99, and Part of #12 again"


def _ref(number: int, owner: str = "o", name: str = "r") -> dict:
    return {
        "id": "I_x", "number": number,
        "repository": {"id": "R_x", "name": name, "owner": {"id": "U_x", "login": owner}},
        "url": f"https://github.com/{owner}/{name}/issues/{number}",
    }


def test_closing_numbers_filters_to_the_repo() -> None:
    refs = [_ref(1), _ref(2, owner="other"), _ref(3, name="elsewhere"), _ref(4, owner="O")]
    assert closing_numbers(refs, "o/r") == [1, 4]


def test_closing_numbers_falls_back_to_the_url_then_counts_an_unknown_home() -> None:
    assert closing_numbers(
        [{"number": 1, "url": "https://github.com/other/repo/issues/1"}], "o/r"
    ) == []
    assert closing_numbers([{"number": 2}], "o/r") == [2]  # a false yes fails loudly
    assert closing_numbers([_ref(3, owner="other")], None) == [3]


def test_closing_numbers_refuses_a_payload_it_cannot_read() -> None:
    with pytest.raises(ValueError, match="not a list"):
        closing_numbers(None, "o/r")


def test_repo_of_pr_url() -> None:
    assert repo_of_pr_url(PR) == "o/r"
    assert repo_of_pr_url("not a url") is None


def test_github_source_hook_reads_only_this_repo_and_its_own_call() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        return json.dumps({"closingIssuesReferences": [_ref(42), _ref(42, owner="other")]})

    assert GitHubIssuesSource("o/r", runner=runner).closing_issue_numbers(PR) == [42]
    assert calls == [["gh", "pr", "view", PR, "--json", "closingIssuesReferences"]]


def test_github_source_hook_raises_when_github_gives_no_answer() -> None:
    source = GitHubIssuesSource("o/r", runner=lambda argv: "{}")
    with pytest.raises(ValueError, match="not a list"):
        source.closing_issue_numbers(PR)


def test_the_shared_pr_read_stays_free_of_the_field() -> None:
    """An old ``gh`` without this field must cost one check, not every task's evidence."""
    from adapters.project.github_issues import _PR_VIEW_FIELDS

    assert "closingIssuesReferences" not in _PR_VIEW_FIELDS


# --- the engine-lane DELIVER runner -----------------------------------------------------


def _cp(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class _Gh:
    """Fake git/gh. ``reads`` answers each closing-references read in order, the last one
    repeating; a value of None makes that read fail. ``existing`` is an already-open PR."""

    def __init__(self, reads, *, existing: bool = False, edit_rc: int = 0, commits: str = "2\n"):
        self.reads = list(reads)
        self.existing = existing
        self.edit_rc = edit_rc
        self.commits = commits
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd=None, **_kw):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return _cp(0, "task/42\n")
        if "rev-list" in argv:
            return _cp(0, self.commits)
        if argv[:3] == ["gh", "pr", "list"]:
            if not self.existing:
                return _cp(0, "[]")
            return _cp(0, json.dumps([{
                "number": 77, "url": PR, "state": "OPEN", "headRefName": "task/42",
                "baseRefName": "main",
            }]))
        if argv[:3] == ["gh", "pr", "create"]:
            return _cp(0, f"{PR}\n")
        if argv[:3] == ["gh", "pr", "edit"]:
            return _cp(self.edit_rc, "", "edit refused" if self.edit_rc else "")
        if argv[:3] == ["gh", "pr", "view"] and "closingIssuesReferences,body" in argv:
            read = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
            return _cp(1, "", "boom") if read is None else _cp(0, json.dumps(read))
        if argv[:3] == ["gh", "pr", "view"]:
            return _cp(0, json.dumps({
                "number": 77, "url": PR, "state": "OPEN", "headRefName": "task/42",
                "baseRefName": "main",
            }))
        return _cp(0)

    def edits(self) -> list[str]:
        return [c[c.index("--body") + 1] for c in self.calls if c[:3] == ["gh", "pr", "edit"]]

    def closing_reads(self) -> int:
        return sum("closingIssuesReferences,body" in c for c in self.calls)


def _closing(*numbers: int, body: str = "Closes #42") -> dict:
    return {"closingIssuesReferences": [_ref(n) for n in numbers], "body": body}


def _work(*, keep: bool = True, context: dict | None = None) -> WorkItem:
    ctx = {"task_id": "#42", "title": "T", "issue_number": 42, **(context or {})}
    if keep:
        ctx["keep_issue_open"] = True
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="#42", stage=Stage.DELIVER, prompt="p",
        schema_ref="deliver", model="engine", lane_policy=_ENGINE_LANE, created_at="now",
        cwd="/wt/42", context=ctx,
    )


def _deliver(gh: _Gh, **kw):
    return DeterministicDeliverRunner(object(), runner=gh).dispatch(_work(**kw))


def test_runner_body_says_part_of_when_set() -> None:
    assert "Part of #42" in _pr_body(_work(), _work().context)
    assert "Closes" not in _pr_body(_work(), _work().context)


def test_runner_body_says_closes_otherwise() -> None:
    work = _work(keep=False)
    assert "Closes #42" in _pr_body(work, work.context)
    assert "Part of" not in _pr_body(work, work.context)


def test_runner_body_rewrites_the_keyword_inside_the_quoted_issue_body() -> None:
    work = _work(context={"body": "Plan: Fixes #42 later. Also Closes #421. Fixes #7."})
    body = _pr_body(work, work.context)
    assert "Part of #42 later" in body
    assert "Closes #421" in body and "Fixes #7" in body
    assert "Fixes #42" not in body


def test_runner_body_leaves_a_quoted_keyword_alone_when_not_set() -> None:
    work = _work(keep=False, context={"body": "Fixes #42 later"})
    assert "Fixes #42 later" in _pr_body(work, work.context)


def test_runner_leaves_a_clean_pr_alone() -> None:
    gh = _Gh([_closing(body="Part of #42")])
    assert _deliver(gh).status is ResultStatus.SUCCESS
    assert gh.edits() == []
    assert gh.closing_reads() == 1


def test_runner_repairs_a_fresh_pr_that_github_says_would_close() -> None:
    """A form the first body cannot rewrite without knowing the repo gets caught here."""
    gh = _Gh([_closing(42, body="Fixes o/r#42"), _closing(body="Part of o/r#42")])
    assert _deliver(gh).status is ResultStatus.SUCCESS
    assert gh.edits() == ["Part of o/r#42"]


def test_runner_repairs_a_reused_pr_it_did_not_write() -> None:
    """A fix cycle re-delivering a PR whose body an earlier run or a person wrote."""
    gh = _Gh([_closing(42, body="Closes #42\n\nDetails"), _closing(body="Part of #42\n\nDetails")],
             existing=True)
    res = _deliver(gh, context={"pr_url": PR, "pr_number": 77})
    assert res.status is ResultStatus.SUCCESS
    assert res.structured_output["reused"] is True
    assert gh.edits() == ["Part of #42\n\nDetails"]
    assert not any(c[:3] == ["gh", "pr", "create"] for c in gh.calls)


def test_runner_repairs_a_reused_pr_with_no_new_commits() -> None:
    gh = _Gh([_closing(42), _closing()], existing=True, commits="0\n")
    res = _deliver(gh, context={"pr_url": PR, "pr_number": 77})
    assert res.status is ResultStatus.SUCCESS
    assert gh.edits() == ["Part of #42"]


def test_runner_fails_when_the_edit_is_refused() -> None:
    gh = _Gh([_closing(42)], edit_rc=1)
    res = _deliver(gh)
    assert res.status is ResultStatus.FAILURE
    assert "#42" in res.error and "edit refused" in res.error


def test_runner_fails_when_the_reference_survives_the_repair() -> None:
    """A manual link in the PR's Development section has no keyword to rewrite."""
    gh = _Gh([_closing(42, body="Part of #42")])
    res = _deliver(gh)
    assert res.status is ResultStatus.FAILURE
    assert "Development" in res.error
    assert gh.edits() == []  # nothing to rewrite, so nothing was written


def test_runner_leaves_an_unreadable_answer_to_the_engine_gate() -> None:
    gh = _Gh([None])
    assert _deliver(gh).status is ResultStatus.SUCCESS
    assert gh.edits() == []


def test_runner_never_reads_back_for_an_ordinary_task() -> None:
    gh = _Gh([_closing(42)])
    assert _deliver(gh, keep=False).status is ResultStatus.SUCCESS
    assert gh.closing_reads() == 0
    assert gh.edits() == []


# --- the engine's check after DELIVER ---------------------------------------------------


def test_violation_text_names_the_issue_and_the_fix() -> None:
    assert keep_open_violation(42, [41, 43]) is None
    assert keep_open_violation(42, []) is None
    reason = keep_open_violation(42, [41, 42])
    assert reason is not None and "#42" in reason and "Part of #42" in reason


def _to_deliver(eng: Engine, *, keep: bool = True) -> WorkItem:
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", keep_issue_open=keep)
    while (work := eng.next_work("r1", "t1")).stage is not Stage.DELIVER:
        eng.record("r1", make_result(work))
    return work


def test_gate_fails_deliver_and_retries_with_the_reason(tmp_path, project) -> None:
    project.task_source.closing_issue_numbers = lambda pr_url: [42]
    eng = _engine(tmp_path, project)
    work = _to_deliver(eng)

    summary = eng.record("r1", make_result(work))

    assert summary["outcome"] != "stage_completed"
    (event,) = _events(eng, "deliver_keep_open_violated")
    assert event["level"] == "warning"
    assert event["issue_number"] == 42
    assert event["closing_issues"] == [42]
    task = eng.store.load_task("r1", "t1")
    assert any("Part of #42" in entry for entry in task.learnings)
    retry = eng.next_work("r1", "t1")
    assert (retry.stage, retry.attempt) == (Stage.DELIVER, work.attempt + 1)


def test_gate_passes_a_clean_pr_with_a_receipt(tmp_path, project) -> None:
    seen: list[str] = []

    def hook(pr_url: str) -> list[int]:
        seen.append(pr_url)
        return [7]

    project.task_source.closing_issue_numbers = hook
    eng = _engine(tmp_path, project)
    work = _to_deliver(eng)
    output = {"pr_number": 77, "pr_url": PR}

    summary = eng.record("r1", make_result(work, structured_output=output))

    assert summary["outcome"] == "stage_completed"
    assert seen == [PR]  # read from the stage's own output, not yet folded on the task
    (event,) = _events(eng, "deliver_keep_open_verified")
    assert event["closing_issues"] == [7]
    assert _events(eng, "deliver_keep_open_violated") == []


def test_gate_fails_open_and_says_so_when_there_is_no_hook(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    work = _to_deliver(eng)

    assert eng.record("r1", make_result(work))["outcome"] == "stage_completed"

    (event,) = _events(eng, "deliver_keep_open_unverified")
    assert event["level"] == "warning"
    assert "closing_issue_numbers" in event["error"]
    assert _events(eng, "deliver_keep_open_verified") == []


def test_gate_fails_open_and_says_so_when_the_read_raises(tmp_path, project) -> None:
    def hook(pr_url: str) -> list[int]:
        raise RuntimeError("gh is down")

    project.task_source.closing_issue_numbers = hook
    eng = _engine(tmp_path, project)
    work = _to_deliver(eng)

    assert eng.record("r1", make_result(work))["outcome"] == "stage_completed"

    (event,) = _events(eng, "deliver_keep_open_unverified")
    assert "gh is down" in event["error"]


def test_an_ordinary_task_is_never_checked(tmp_path, project) -> None:
    calls: list[str] = []

    def hook(pr_url: str) -> list[int]:
        calls.append(pr_url)
        return [42]

    project.task_source.closing_issue_numbers = hook
    eng = _engine(tmp_path, project)
    work = _to_deliver(eng, keep=False)

    assert eng.record("r1", make_result(work))["outcome"] == "stage_completed"

    while (next_work := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(next_work))
    assert eng.store.load_task("r1", "t1").state is TaskState.COMPLETED
    assert calls == []
    assert not [e for e in eng.store.read_events("r1") if "keep_open" in str(e["type"])]


def test_a_failed_deliver_is_not_read_back(tmp_path, project) -> None:
    calls: list[str] = []
    project.task_source.closing_issue_numbers = lambda pr_url: calls.append(pr_url) or []
    eng = _engine(tmp_path, project)
    work = _to_deliver(eng)

    eng.record("r1", make_result(work, structured_output={"pr_number": 0, "pr_url": "nope"}))

    assert calls == []


# --- the check at completion ------------------------------------------------------------


def _finish(eng: Engine) -> None:
    while (work := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(work))


def test_completion_reports_a_keyword_that_came_back_after_deliver(tmp_path, project) -> None:
    answers = iter([[], [42]])  # clean at DELIVER, rewritten by the time the task completes
    project.task_source.closing_issue_numbers = lambda pr_url: next(answers)
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", keep_issue_open=True)

    _finish(eng)

    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.COMPLETED  # report-only: a completed task stays completed
    (event,) = _events(eng, "completion_pr_invalid")
    assert "Part of #42" in event["error"]
    audit = eng.status("r1")["completion_notes"]
    assert audit["delivery_invalid_by_task"] == {"t1": 1}
    (completed,) = [
        e for e in _events(eng, "notification") if e.get("kind") == "task_completed"
    ]
    assert "Part of #42" in str(completed)


def test_completion_is_clean_when_the_pr_still_does_not_close(tmp_path, project) -> None:
    project.task_source.closing_issue_numbers = lambda pr_url: []
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", keep_issue_open=True)

    _finish(eng)

    assert _events(eng, "completion_pr_invalid") == []
    assert _events(eng, "completion_pr_validated")


def test_completion_says_so_when_it_cannot_read(tmp_path, project) -> None:
    answers: list = [[], None]

    def hook(pr_url: str) -> list[int]:
        answer = answers.pop(0)
        if answer is None:
            raise RuntimeError("gh is down")
        return answer

    project.task_source.closing_issue_numbers = hook
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", keep_issue_open=True)

    _finish(eng)

    (event,) = _events(eng, "deliver_keep_open_unverified")
    assert event["checked_at"] == "completion"
    assert "gh is down" in event["error"]
    assert _events(eng, "completion_pr_invalid") == []  # unread is not a finding
    assert eng.store.load_task("r1", "t1").state is TaskState.COMPLETED


@pytest.mark.parametrize("skills_dir", [".claude", ".agents"])
def test_batch_plan_skill_tells_the_planner_when_to_set_it(skills_dir) -> None:
    """The field only helps if the planner reaches for it: two of the three `pred-1300-1329`
    closures were knowable at planning time."""
    root = Path(__file__).resolve().parents[1]
    skill = " ".join((root / skills_dir / "skills/batch-plan/SKILL.md").read_text().split())
    assert "`keep_issue_open`" in skill
    assert "merging the PR cannot meet the issue's done-when" in skill
