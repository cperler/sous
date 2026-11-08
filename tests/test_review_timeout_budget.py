"""#624 — REVIEW's 10-minute limit can be raised, and a timeout no longer loses its work.

prediction#1365 (a 4,579-line change) timed out in REVIEW twice. Neither attempt was stuck,
both had found real bugs, and the task failed by the breaker with no verdict. Now:

- a task carries a ``review_budget`` (pinned at add-task / in a batch plan), a project may
  declare its own REVIEW default, and an unpinned large diff is reviewed on the long limit;
- a REVIEW timeout raises the budget to ``long`` and hands the next attempt a digest of what
  the timed-out one ran, read from its retained stream;
- a REVIEW that times out on its last attempt is held for a human, not failed.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.batch_plan import BatchPlanError, apply_plan, validate_plan
from orchestrator.cli import main
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine, review_timeout_s
from orchestrator.schemas.enums import (
    ExecutionLane,
    ResultStatus,
    ReviewBudget,
    Stage,
    StageStatus,
    TaskState,
)
from orchestrator.stages import STAGE_SPECS, DiffStat, render_prompt, render_review_plan
from orchestrator.status_store import StatusStore
from orchestrator.stream_probe import attempt_stream_paths, review_progress_digest, stages_dir
from tests.conftest import FakeProject, make_result

B = ReviewBudget
SPEC = STAGE_SPECS[Stage.REVIEW].timeout_s


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project)


def _events(eng: Engine, etype: str, run: str = "r1") -> list[dict]:
    return [e for e in eng.store.read_events(run) if e["type"] == etype]


def _work_until(eng: Engine, stage: Stage, task: str = "t1"):
    while (work := eng.next_work("r1", task)).stage is not stage:
        eng.record("r1", make_result(work))
    return work


def _diff(monkeypatch, lines: int | None) -> None:
    """Stub the engine's own diff measurement (the fake project has no real worktree)."""
    stat = None if lines is None else DiffStat(files=3, lines=lines)
    monkeypatch.setattr(Engine, "_deterministic_diff_stat", lambda self, task: stat)


def _tool_use(tid: str, name: str, **inp) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}})


def _tool_result(tid: str, text: str, *, error: bool = False) -> str:
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": error,
         "content": [{"type": "text", "text": text}]}]}})


def _say(text: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}})


STREAM = "\n".join([
    json.dumps({"type": "system", "subtype": "init"}),
    _tool_use("a", "Read", file_path="src/lib.py"),
    _tool_result("a", "def f(): ..."),
    _say("Suspected bug: f() drops the last row."),
    _tool_use("b", "Bash", command="uv run pytest tests/test_lib.py -q"),
    _tool_result("b", "1 failed, 12 passed", error=True),
    _tool_use("c", "Bash", command="sed -i s/</<=/ src/lib.py && uv run pytest -q"),
    '{"type": "user", "message": {"content": [{"type": "tool_res',  # cut off mid-write
])


# --- the limit each budget selects ------------------------------------------------------------


@pytest.mark.parametrize(
    ("budget", "project_default", "diff_lines", "expected", "reason"),
    [
        (None, None, None, SPEC, "spec_default"),
        (None, None, 200, SPEC, "spec_default"),
        (None, None, 1500, 1800, "diff_size"),
        (B.STANDARD, None, 5000, SPEC, "spec_default"),  # an explicit pin turns scaling off
        (B.LONG, None, None, 1800, "budget_long"),
        (None, 1200, None, 1200, "project_default"),
        (B.LONG, 1200, None, 1800, "budget_long"),
        (B.LONG, 2400, None, 2400, "budget_long"),  # more time never shortens the limit
        (None, 2400, 9999, 2400, "diff_size"),
    ],
)
def test_budget_precedence(budget, project_default, diff_lines, expected, reason) -> None:
    assert SPEC == 600
    assert review_timeout_s(
        budget, spec_timeout_s=SPEC, project_default_s=project_default, diff_lines=diff_lines
    ) == (expected, reason)


# --- the dispatched limit --------------------------------------------------------------------


def test_a_small_diff_keeps_the_default_and_says_nothing(tmp_path, project, monkeypatch) -> None:
    _diff(monkeypatch, 40)
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    assert _work_until(eng, Stage.REVIEW).timeout_s == SPEC
    assert _events(eng, "review_timeout_set") == []


def test_a_large_diff_gets_the_long_limit(tmp_path, project, monkeypatch) -> None:
    _diff(monkeypatch, 4579)
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    assert _work_until(eng, Stage.REVIEW).timeout_s == 1800
    (ev,) = _events(eng, "review_timeout_set")
    assert ev["reason"] == "diff_size" and ev["diff_lines"] == 4579 and ev["budget"] is None


def test_an_unmeasured_diff_keeps_the_default(tmp_path, project, monkeypatch) -> None:
    _diff(monkeypatch, None)
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    assert _work_until(eng, Stage.REVIEW).timeout_s == SPEC


def test_the_limit_only_moves_review(tmp_path, project, monkeypatch) -> None:
    _diff(monkeypatch, 4579)
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", review_budget="long")
    seen: dict[Stage, int | None] = {}
    while (work := eng.next_work("r1", "t1")).stage is not Stage.REVIEW:
        seen[work.stage] = work.timeout_s
        eng.record("r1", make_result(work))
    assert work.timeout_s == 1800
    assert seen[Stage.SCOPE] == STAGE_SPECS[Stage.SCOPE].timeout_s
    assert seen[Stage.DELIVER] == STAGE_SPECS[Stage.DELIVER].timeout_s


def test_project_default_applies(tmp_path, project, monkeypatch) -> None:
    _diff(monkeypatch, 10)
    project.review_timeout_s = 1200
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    assert _work_until(eng, Stage.REVIEW).timeout_s == 1200
    (ev,) = _events(eng, "review_timeout_set")
    assert ev["reason"] == "project_default"
    assert _events(eng, "review_timeout_rejected") == []


@pytest.mark.parametrize("bad", [0, -5, "1200", 1.5, True])
def test_unusable_project_default_is_evented_and_ignored(
    tmp_path, project, monkeypatch, bad
) -> None:
    _diff(monkeypatch, 10)
    project.review_timeout_s = bad
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    assert _work_until(eng, Stage.REVIEW).timeout_s == SPEC
    (ev,) = _events(eng, "review_timeout_rejected")
    assert ev["level"] == "warning" and ev["value"] == repr(bad)


# --- add-task / batch-plan pins ---------------------------------------------------------------


def test_add_task_cli_pin_persists_across_engine_rebuilds(tmp_path, capsys) -> None:
    base = ["--root", str(tmp_path), "--run", "run1", "--project", "tests.fakeproject"]
    assert main([*base, "init-run", "--lane", "full"]) == 0
    capsys.readouterr()
    assert main([*base, "add-task", "--task", "#42", "--review-budget", "long"]) == 0
    assert json.loads(capsys.readouterr().out)["review_budget"] == "long"
    store = StatusStore(tmp_path)
    assert store.load_task("run1", "#42").review_budget is B.LONG
    eng = Engine(store, CostLedger(tmp_path / "stage-costs.jsonl"), FakeProject())
    while (work := eng.next_work("run1", "#42")).stage is not Stage.REVIEW:
        eng.record("run1", make_result(work))
    assert work.timeout_s == 1800


def test_add_task_cli_rejects_unknown_budget(tmp_path) -> None:
    base = ["--root", str(tmp_path), "--run", "run1", "--project", "tests.fakeproject"]
    assert main([*base, "init-run", "--lane", "full"]) == 0
    with pytest.raises(SystemExit):
        main([*base, "add-task", "--task", "#42", "--review-budget", "huge"])


def _plan(**task) -> dict:
    return {"tasks": [{"task_id": "#1", "pipeline": "full", **task}]}


def test_batch_plan_carries_the_budget(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    preview = apply_plan(eng, "r1", _plan(review_budget="long"), known_ids=["#1"], dry_run=True)
    assert preview["added"][0]["review_budget"] == "long"
    apply_plan(eng, "r1", _plan(review_budget="long"), known_ids=["#1"])
    assert eng.store.load_task("r1", "#1").review_budget is B.LONG


def test_batch_plan_schema_refuses_an_unknown_budget() -> None:
    validate_plan(_plan(review_budget=None), known_ids=["#1"])  # no raise
    with pytest.raises(BatchPlanError, match="review_budget"):
        validate_plan(_plan(review_budget="huge"), known_ids=["#1"])


# --- a timed-out REVIEW -----------------------------------------------------------------------


def _write_stream(tmp_path, name: str, text: str = STREAM) -> None:
    d = stages_dir(tmp_path, "t1")
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def test_a_review_timeout_retries_long_and_continues(tmp_path, project, monkeypatch) -> None:
    _diff(monkeypatch, 40)
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    work = _work_until(eng, Stage.REVIEW)
    assert work.timeout_s == SPEC
    _write_stream(tmp_path, "review-attempt0.stream.jsonl")
    eng.record("r1", make_result(work, status=ResultStatus.TIMEOUT, error="timed out after 600s"))

    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.RETRYING
    assert task.review_budget is B.LONG
    (raised,) = _events(eng, "review_budget_raised")
    assert raised["from"] is None and raised["to"] == "long" and raised["attempt"] == 0
    (carried,) = _events(eng, "review_progress_carried")
    assert carried["carried"] == 3 and carried["streams"] == 1 and "reason" not in carried
    assert "uv run pytest tests/test_lib.py -q" in task.review_progress
    assert "1 failed, 12 passed" in task.review_progress
    assert "Suspected bug: f() drops the last row." in task.review_progress

    retry = eng.next_work("r1", "t1")
    assert retry.stage is Stage.REVIEW and retry.attempt == 1
    assert retry.timeout_s == 1800
    assert "## Progress from the timed-out review" in retry.prompt
    assert "1 failed, 12 passed" in retry.prompt

    # A review that returns a result consumes the carried progress.
    eng.record("r1", make_result(retry))
    task = eng.store.load_task("r1", "t1")
    assert task.review_progress is None
    assert task.state is TaskState.COMPLETED


def test_two_review_timeouts_hold_for_a_human_not_fail(tmp_path, project, monkeypatch) -> None:
    _diff(monkeypatch, 40)
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    work = _work_until(eng, Stage.REVIEW)
    # The same error text twice trips the breaker, as prediction#1365's did.
    eng.record("r1", make_result(work, status=ResultStatus.TIMEOUT, error="timed out"))
    work = eng.next_work("r1", "t1")
    _write_stream(tmp_path, "review-attempt1.stream.jsonl")
    eng.record("r1", make_result(work, status=ResultStatus.TIMEOUT, error="timed out"))

    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.BLOCKED_ON_HUMAN
    assert task.stages[Stage.REVIEW].status is StageStatus.FAILED
    assert "needs a human review" in task.last_error
    assert task.error_signatures == []
    recorded = [e for e in _events(eng, "stage_recorded") if e.get("stage") == "review"]
    assert recorded[-1]["outcome"] == "review_timed_out_held"
    # The first timeout had no stream (an interactive lane): evented, never silent.
    first, second = _events(eng, "review_progress_carried")
    assert first["carried"] == 0 and first["reason"] == "no_stream"
    assert second["carried"] == 3
    assert "### Attempt 1" in task.review_progress
    notes = [e for e in _events(eng, "notification") if e.get("kind") == "task_blocked"]
    assert notes and "needs a human review" in json.dumps(notes[-1])

    # A human release re-dispatches REVIEW, still on the long limit and with the progress.
    eng.approve("r1", "t1", approved_by="craig")
    again = eng.next_work("r1", "t1")
    assert again.stage is Stage.REVIEW and again.timeout_s == 1800
    assert "1 failed, 12 passed" in again.prompt


def test_running_out_of_attempts_also_holds(tmp_path, project, monkeypatch) -> None:
    _diff(monkeypatch, 40)
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    work = _work_until(eng, Stage.REVIEW)
    for attempt in range(3):  # distinct errors: no breaker, so max_attempts ends it
        assert work.stage is Stage.REVIEW and work.attempt == attempt
        eng.record("r1", make_result(work, status=ResultStatus.TIMEOUT, error=f"e{'x' * attempt}"))
        if attempt < 2:
            work = eng.next_work("r1", "t1")
    assert eng.store.load_task("r1", "t1").state is TaskState.BLOCKED_ON_HUMAN


def test_a_non_review_timeout_still_fails_as_before(tmp_path, project, monkeypatch) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    for _ in range(2):
        work = _work_until(eng, Stage.SCOPE)
        eng.record("r1", make_result(work, status=ResultStatus.TIMEOUT, error="timed out"))
    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.FAILED
    assert task.review_budget is None and task.review_progress is None
    assert _events(eng, "review_progress_carried") == []


# --- the stream digest ------------------------------------------------------------------------


def test_digest_pairs_calls_with_results_and_tolerates_a_cut_line() -> None:
    d = review_progress_digest([("review-attempt0", STREAM)])
    assert d["steps"] == 3 and d["dropped_steps"] == 0 and d["notes"] == 1
    text = d["text"]
    assert "1. Read: src/lib.py" in text and "result: def f(): ..." in text
    assert "result: [error] 1 failed, 12 passed" in text
    # The call the timeout killed has no result, and says so.
    assert "3. Bash: sed -i" in text and "result: (none: cut off)" in text


def test_digest_keeps_the_most_recent_steps_and_reports_the_rest() -> None:
    lines = []
    for i in range(10):
        lines += [_tool_use(f"t{i}", "Bash", command=f"step {i}"), _tool_result(f"t{i}", "x" * 50)]
    d = review_progress_digest([("s", "\n".join(lines))], max_steps=4, max_result_chars=10)
    assert d["steps"] == 4 and d["dropped_steps"] == 6
    assert "6 earlier step(s) omitted" in d["text"]
    assert "step 5" not in d["text"] and "step 9" in d["text"]
    assert "[40 chars cut]" in d["text"]


def test_digest_reads_codex_commands() -> None:
    stream = "\n".join([
        json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "pytest -q", "aggregated_output": "boom",
            "exit_code": 1}}),
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "the guard is missing"}}),
    ])
    d = review_progress_digest([("s", stream)])
    assert "command: pytest -q" in d["text"] and "[exit 1] boom" in d["text"]
    assert "the guard is missing" in d["text"]


def test_digest_of_nothing_is_empty() -> None:
    d = review_progress_digest([("s", json.dumps({"type": "system"}))])
    assert d["text"] == "" and d["steps"] == 0


def test_attempt_stream_paths_matches_one_attempt_and_cycle(tmp_path) -> None:
    for name in [
        "review-attempt1.stream.jsonl",
        "review-attempt1.find-code.stream.jsonl",
        "review-attempt1.retry1.stream.jsonl",
        "review-attempt1.cycle2.stream.jsonl",
        "review-attempt10.stream.jsonl",
        "review-attempt0.stream.jsonl",
        "implement-attempt1.stream.jsonl",
    ]:
        _write_stream(tmp_path, name, "")
    names = [p.name for p in attempt_stream_paths(tmp_path, "t1", "review", 1)]
    assert names == [
        "review-attempt1.find-code.stream.jsonl",
        "review-attempt1.retry1.stream.jsonl",
        "review-attempt1.stream.jsonl",
    ]
    cycled = [p.name for p in attempt_stream_paths(tmp_path, "t1", "review", 1, cycle=2)]
    assert cycled == ["review-attempt1.cycle2.stream.jsonl"]


# --- the prompt -------------------------------------------------------------------------------


def test_progress_reaches_review_prompts_only() -> None:
    review = render_prompt(Stage.REVIEW, task_id="t1", title="t", body="b",
                           review_progress="ran X")
    assert "## Progress from the timed-out review" in review and "ran X" in review
    plain = render_prompt(Stage.REVIEW, task_id="t1", title="t", body="b")
    assert "Progress from the timed-out review" not in plain
    implement = render_prompt(Stage.IMPLEMENT, task_id="t1", title="t", body="b",
                              review_progress="ran X")
    assert "ran X" not in implement


def test_progress_reaches_every_panel_finder() -> None:
    plan = render_review_plan(task_id="t1", title="t", body="b", review_progress="ran X")
    assert plan.finders and all("ran X" in f.prompt for f in plan.finders)
