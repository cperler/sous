"""Task context plane — the fold (review Phase B #5; 2026-07-01 design note).

Every stage's whitelisted structured-output fields are folded into an engine-owned,
bounded, injective `task.context`. Correctness is derived from durable results only.
"""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionLane, Stage
from orchestrator.state_machine import (
    _MAX_CONTEXT_BYTES,
    _MAX_ITEM_STR,
    _MAX_LIST,
    _MAX_PLAN_ITEM_STR,
    _MAX_STR,
    CONTEXT_KEYS,
    _absorb_outputs,
    _context_bytes,
    _enforce_context_ceiling,
)
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project)


def test_context_keys_are_injective_across_stages() -> None:
    seen: set[str] = set()
    for keys in CONTEXT_KEYS.values():
        for k in keys:
            assert k not in seen, f"context key {k!r} written by more than one stage"
            seen.add(k)


def test_full_run_folds_every_stage_into_context(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))
    ctx = eng.store.load_task("r1", "t1").context
    # scope's plan reaches the task context (was dropped on the floor before)
    assert ctx["plan"] == ["subtask-1"]
    # intake's worktree/branch, implement's summary, test's notes, deliver's pr all folded
    assert ctx["branch"] == "issue-42" and ctx["worktree"] == "/wt/42"
    assert ctx["summary"] == "done" and ctx["files_changed"] == ["a.py"]
    assert ctx["tests_meaningful"] is True
    assert ctx["validation_notes"] == "asserts the changed behavior"
    assert ctx["pr_url"].endswith("/1234") and ctx["pr_number"] == 1234
    # context survives on the persisted task doc (resume/replay safe)
    assert "plan" in eng.store.load_task("r1", "t1").context


def test_intake_composed_deps_folds_into_context() -> None:
    # #232: composed_deps (the #216 dep branches merged at intake) must reach task.context
    # so the DELIVER stage can annotate the PR with them. Fails if composed_deps is not in
    # CONTEXT_KEYS[INTAKE] (it would be dropped at the fold and never reach deliver).
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    _absorb_outputs(task, make_result_stub(
        Stage.INTAKE, {"branch": "b", "worktree": "/wt", "composed_deps": ["task/dep1"]}))
    assert task.context["composed_deps"] == ["task/dep1"]


def test_fold_is_tolerant_of_missing_keys() -> None:
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    res = make_result_stub(Stage.SCOPE, {"feasible": True})  # no "plan" key
    _absorb_outputs(task, res)
    assert task.context == {}  # nothing to fold, no crash


# --- #289: caps are by FIELD MEANING, and never silent ---------------------------------
def _prose_plan(n: int, chars: int = 800) -> list[str]:
    """A realistic SCOPE plan: n ordered prose subtask instructions, each `chars` long,
    with the caveat that matters LAST (exactly what the 500-char cap used to delete)."""
    tail = " — minding the existing task-lock -> events-lock ordering."
    return [
        f"Subtask {i}: " + ("w" * (chars - len(tail) - len(f"Subtask {i}: "))) + tail
        for i in range(n)
    ]


def test_scope_plan_subtasks_survive_the_fold_intact() -> None:
    """#289 acceptance: a 9-subtask plan well under the 16KB ceiling reaches the context
    BYTE-IDENTICAL. Before the fix each >500-char subtask was cut mid-sentence, losing the
    trailing caveats, with no space pressure whatsoever."""
    from orchestrator.schemas.status import Task

    plan = _prose_plan(9)
    assert all(len(s) > _MAX_ITEM_STR for s in plan)  # every subtask beat the old cap
    assert _context_bytes({"plan": plan}) < _MAX_CONTEXT_BYTES  # ~3.6KB: no space pressure

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    notices = _absorb_outputs(task, make_result_stub(Stage.SCOPE, {"plan": plan}))
    assert task.context["plan"] == plan  # every subtask intact, tail caveat included
    assert not any("[truncated]" in s for s in task.context["plan"])
    assert notices.truncations == [] and notices.evictions == []  # nothing to report


def test_oversized_plan_subtask_truncates_with_a_notice() -> None:
    """#289 acceptance: a genuinely oversized subtask IS still bounded — and the fold
    RETURNS a notice naming the field, the element and how many chars went (the engine
    turns it into a context_value_truncated event)."""
    from orchestrator.schemas.status import Task

    over = "z" * (_MAX_PLAN_ITEM_STR + 600)
    plan = ["fine subtask", over]
    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    notices = _absorb_outputs(task, make_result_stub(Stage.SCOPE, {"plan": plan}))
    assert task.context["plan"][0] == "fine subtask"  # the in-budget sibling is untouched
    assert task.context["plan"][1].endswith("[truncated]")
    assert len(notices.truncations) == 1
    (notice,) = notices.truncations
    assert notice["field"] == "plan"
    assert notice["part"] == "item[1]"  # names WHICH subtask was cut
    assert notice["kept_chars"] == _MAX_PLAN_ITEM_STR
    assert notice["dropped_chars"] == 600


def test_non_plan_list_items_keep_the_default_item_cap() -> None:
    """#289 raises the cap for `plan` by FIELD MEANING — it must not raise the default for
    incidental list values (review findings, file paths), which stay at _MAX_ITEM_STR."""
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    issues = ["y" * (_MAX_ITEM_STR + 120)]
    notices = _absorb_outputs(task, make_result_stub(Stage.REVIEW, {"issues": issues}))
    assert task.context["issues"][0].endswith("[truncated]")
    assert len(task.context["issues"][0]) == _MAX_ITEM_STR + len(" … [truncated]")
    (notice,) = notices.truncations
    assert notice["field"] == "issues" and notice["dropped_chars"] == 120


def test_whole_string_truncation_returns_a_notice() -> None:
    """#289: the whole-string cap (_MAX_STR) reports what it cut too, not just list items."""
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    notices = _absorb_outputs(task, make_result_stub(
        Stage.IMPLEMENT, {"summary": "s" * (_MAX_STR + 77), "files_changed": []}))
    (notice,) = notices.truncations
    assert notice["field"] == "summary" and notice["part"] == "value"
    assert notice["kept_chars"] == _MAX_STR and notice["dropped_chars"] == 77


def test_list_tail_truncation_returns_a_notice() -> None:
    """#289: dropping the tail of an over-long list is reported as an item COUNT."""
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    notices = _absorb_outputs(task, make_result_stub(
        Stage.SCOPE, {"plan": [f"step-{i}" for i in range(_MAX_LIST + 5)]}))
    assert len(task.context["plan"]) == _MAX_LIST + 1  # kept + the "… (N more)" marker
    (notice,) = notices.truncations
    assert notice["field"] == "plan" and notice["part"] == "list_tail"
    assert notice["kept_items"] == _MAX_LIST and notice["dropped_items"] == 5


def test_bigger_plan_cap_still_respects_the_context_ceiling() -> None:
    """#289: the raised per-item cap is NOT unbounded growth — the whole-context ceiling
    still binds, and the eviction it performs is itself reported (never silent)."""
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    _absorb_outputs(task, make_result_stub(Stage.INTAKE, {"branch": "b", "worktree": "/wt"}))
    huge_plan = _prose_plan(6, chars=_MAX_PLAN_ITEM_STR)  # ~24KB > the 16KB ceiling
    assert _context_bytes({"plan": huge_plan}) > _MAX_CONTEXT_BYTES
    notices = _absorb_outputs(task, make_result_stub(Stage.SCOPE, {"plan": huge_plan}))
    assert _context_bytes(task.context) <= _MAX_CONTEXT_BYTES  # ceiling held
    assert "plan" not in task.context  # the fat key is shed
    assert task.context["branch"] == "b"  # intake survives
    assert [n["field"] for n in notices.evictions] == ["plan"]  # the loss is reported
    assert notices.evictions[0]["bytes"] > _MAX_CONTEXT_BYTES


def test_implement_prompt_carries_the_full_scope_plan(tmp_path, project) -> None:
    """#289 end-to-end acceptance: a 9-subtask prose plan reaches the DISPATCHED implement
    prompt intact — the live failure was the implementer reading a plan whose caveats had
    been cut before it ever saw them."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    plan = _prose_plan(9)
    while (w := eng.next_work("r1", "t1")) is not None:
        if w.stage is Stage.IMPLEMENT:
            # the model lane reads the PROMPT (WorkItem.context is for engine-lane runners)
            for subtask in plan:
                assert subtask in w.prompt  # each subtask rendered WHOLE, caveat and all
            assert "[truncated]" not in w.prompt
            assert eng.store.load_task("r1", "t1").context["plan"] == plan  # durable too
            return
        output = {"feasible": True, "plan": plan} if w.stage is Stage.SCOPE else None
        eng.record("r1", make_result(w, structured_output=output))
    raise AssertionError("implement stage never dispatched")


def test_string_and_list_values_are_capped() -> None:
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    big_summary = "x" * (_MAX_STR + 500)
    big_plan = [f"step-{i}" for i in range(_MAX_LIST + 20)]
    _absorb_outputs(task, make_result_stub(Stage.IMPLEMENT, {"summary": big_summary,
                                                             "files_changed": []}))
    _absorb_outputs(task, make_result_stub(Stage.SCOPE, {"plan": big_plan}))
    assert len(task.context["summary"]) <= _MAX_STR + len(" … [truncated]")
    assert task.context["summary"].endswith("[truncated]")
    assert len(task.context["plan"]) == _MAX_LIST + 1  # kept + the "… (N more)" marker
    assert task.context["plan"][-1].startswith("… (")


def test_context_ceiling_drops_lowest_priority_stages_first() -> None:
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    # intake contributes a little; review contributes a maxed list (~40*500 ≈ 20KB) that
    # alone blows the 16KB ceiling, so the ceiling must drop review (lowest priority)
    # while keeping intake (highest priority).
    _absorb_outputs(task, make_result_stub(Stage.INTAKE, {"branch": "b", "worktree": "/wt"}))
    huge_issues = ["z" * _MAX_ITEM_STR for _ in range(_MAX_LIST)]
    _absorb_outputs(task, make_result_stub(Stage.REVIEW, {"issues": huge_issues}))
    assert _context_bytes(task.context) <= _MAX_CONTEXT_BYTES  # ceiling held
    assert "branch" in task.context and "worktree" in task.context  # earliest kept
    assert "issues" not in task.context  # review dropped first (lowest priority)


def test_context_ceiling_evicts_largest_stage_not_reverse_order() -> None:
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    # A fat test.failures list sized to sit JUST UNDER the ceiling on its own: 33 elements
    # of len 490 plus one of len 30 (every element < _MAX_ITEM_STR and count < _MAX_LIST, so
    # nothing is truncated). Deterministic byte-count ≈ 16350 < 16384.
    failures = ["f" * 489 + str(i % 10) for i in range(33)] + ["g" * 30]
    _absorb_outputs(task, make_result_stub(Stage.TEST, {"failures": failures}))
    assert _context_bytes(task.context) <= _MAX_CONTEXT_BYTES  # test fold alone fits
    # deliver adds a TINY contribution (~65 bytes) that tips the whole context over the
    # ceiling. Size-aware eviction must shed the fat test.failures, not the small deliver
    # keys. The pre-fix reverse-pipeline order evicted the PR-bearing stage first (it
    # precedes TEST in reversed(STAGE_ORDER)), starving the near-ceiling test.failures —
    # the bug this guards.
    _absorb_outputs(
        task,
        make_result_stub(
            Stage.PUBLISH,
            {"pr_number": 1234, "pr_url": "https://github.com/o/r/pull/1234"},
        ),
    )
    assert _context_bytes(task.context) <= _MAX_CONTEXT_BYTES  # ceiling held
    assert "failures" not in task.context  # the FAT contribution is evicted first
    assert task.context["pr_number"] == 1234  # the small publish keys SURVIVE
    assert task.context["pr_url"].endswith("/1234")


def test_context_ceiling_evicts_the_fat_key_but_keeps_its_small_siblings() -> None:
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    # A single fat test.failures list that alone tips the ceiling, folded ALONGSIDE its
    # small same-stage siblings (tests_meaningful, validation_notes). Per-KEY eviction must
    # shed only the fat `failures` key; whole-stage eviction would have dropped the whole
    # TEST contribution, needlessly losing the two small siblings.
    failures = ["f" * 489 + str(i % 10) for i in range(34)]  # ~16.8KB > ceiling on its own
    _absorb_outputs(
        task,
        make_result_stub(
            Stage.TEST,
            {
                "failures": failures,
                "tests_meaningful": True,
                "validation_notes": "asserts the changed behavior",
            },
        ),
    )
    assert _context_bytes(task.context) <= _MAX_CONTEXT_BYTES  # ceiling held
    assert "failures" not in task.context  # the FAT key is evicted
    assert task.context["tests_meaningful"] is True  # small siblings SURVIVE
    assert task.context["validation_notes"] == "asserts the changed behavior"


def test_context_ceiling_tie_break_evicts_latest_pipeline_stage_first() -> None:
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    # Two folds engineered to weigh EXACTLY the same: SCOPE.plan (early pipeline) and
    # REVIEW.issues (latest pipeline). The 2-byte gap between the "plan"/"issues" key names
    # is offset by making one plan element 2 chars longer, so `_weight` cannot choose by
    # bytes — only the reverse-pipeline tie-break (max over reversed(STAGE_ORDER)) decides,
    # and the docstring promises it evicts the latest-pipeline stage's key first.
    plan = ["z" * 498 for _ in range(19)] + ["z" * 500]
    issues = ["z" * 498 for _ in range(20)]
    assert _context_bytes({"plan": plan}) == _context_bytes({"issues": issues})  # true tie
    _absorb_outputs(task, make_result_stub(Stage.SCOPE, {"plan": plan}))  # ~10KB, fits alone
    assert _context_bytes(task.context) <= _MAX_CONTEXT_BYTES
    _absorb_outputs(task, make_result_stub(Stage.REVIEW, {"issues": issues}))  # tips ceiling
    assert _context_bytes(task.context) <= _MAX_CONTEXT_BYTES  # ceiling held
    assert "issues" not in task.context  # REVIEW (latest pipeline) evicted on the tie
    assert task.context["plan"] == plan  # the equal-weight EARLIER stage's key survives


def test_context_ceiling_evicts_across_multiple_passes_via_absorb() -> None:
    """#37: drive the multi-pass sweep through the REAL fold path (_absorb_outputs), not a
    hand-seeded task.context — so this covers the production call site and would catch a
    break in the absorb→enforce integration for the multi-pass scenario, not just the
    ceiling function in isolation."""
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    # Fold in pipeline order. SCOPE.plan + IMPLEMENT.files_changed land a stable prior
    # context that sits JUST under the ceiling with BOTH keys present (no eviction yet).
    plan = ["p" * 498 for _ in range(22)]  # SCOPE, ~11KB
    files_changed = ["m" * 474 for _ in range(11)]  # IMPLEMENT, ~5.3KB
    _absorb_outputs(task, make_result_stub(Stage.SCOPE, {"plan": plan}))
    _absorb_outputs(task, make_result_stub(Stage.IMPLEMENT, {"files_changed": files_changed}))
    prior_bytes = _context_bytes(task.context)
    assert prior_bytes <= _MAX_CONTEXT_BYTES  # prior fits: nothing evicted at the folds above
    assert {"plan", "files_changed"} <= set(task.context)  # both survive into the prior state

    # Now a single TEST fold tips the context WAY over. Its lone _enforce_context_ceiling
    # call must run >1 pass: evicting the heaviest (failures) alone still leaves plan +
    # files_changed above the ceiling (prior was near-ceiling), so the sweep keeps going and
    # evicts plan too — heaviest-first — while the lighter files_changed and the small TEST
    # siblings survive. One eviction could not have sufficed.
    failures = ["z" * 498 for _ in range(28)]  # TEST, ~14KB (heaviest)
    _absorb_outputs(
        task,
        make_result_stub(
            Stage.TEST,
            {
                "failures": failures,
                "validation_notes": "asserts the changed behavior",
                "tests_meaningful": True,
            },
        ),
    )
    assert _context_bytes(task.context) <= _MAX_CONTEXT_BYTES  # ceiling held
    # >1 pass: the two heaviest keys are both gone (dropping `failures` alone would have left
    # plan + files_changed still over, since the prior state was near-ceiling).
    assert "failures" not in task.context
    assert "plan" not in task.context
    # the lighter fat key and the small same-stage siblings survive the multi-pass sweep.
    assert task.context["files_changed"] == files_changed
    assert task.context["validation_notes"] == "asserts the changed behavior"
    assert task.context["tests_meaningful"] is True


def test_context_ceiling_eviction_order_is_characterized() -> None:
    """Characterization lock (#26 perf refactor must not change observable behavior): the
    exact eviction ORDER of _enforce_context_ceiling for a fixed over-ceiling context.
    Pins heaviest-first eviction AND the reverse-pipeline tie-break in one shot — `issues`
    (REVIEW) and `plan` (SCOPE) weigh EXACTLY the same, and both are heavier than
    `failures`; both tie keys are evicted (reverse-pipeline: REVIEW's `issues` first, then
    SCOPE's `plan`) while the lighter `failures` and the tiny `branch` survive."""
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    failures = ["z" * 480 for _ in range(20)]  # TEST, ~9.7KB
    issues = ["z" * 498 for _ in range(20)]  # REVIEW  ┐ engineered exact byte-tie:
    plan = ["z" * 498 for _ in range(19)] + ["z" * 500]  # SCOPE  ┘ heavier than failures
    files_changed = ["m" * 480 for _ in range(10)]  # IMPLEMENT, ~4.9KB (survives)
    task.context = {
        "branch": "b",  # INTAKE, tiny — always survives
        "files_changed": files_changed,
        "plan": plan,
        "issues": issues,
        "failures": failures,
    }
    assert _context_bytes({"issues": issues}) == _context_bytes({"plan": plan})  # true tie
    assert _context_bytes(task.context) > _MAX_CONTEXT_BYTES
    _enforce_context_ceiling(task)
    assert _context_bytes(task.context) <= _MAX_CONTEXT_BYTES  # ceiling held
    # both equal-weight heavy keys evicted; the lighter failures + tiny branch + medium
    # files_changed survive. This is the observable contract the #26 refactor preserves.
    assert set(task.context) == {"branch", "files_changed", "failures"}


def _prompt_at(eng: Engine, stage: Stage, run="r1", task="t1") -> str:
    """Drive the task until `stage` is dispatched; return that WorkItem's prompt."""
    while (w := eng.next_work(run, task)) is not None:
        if w.stage is stage:
            return w.prompt
        eng.record(run, make_result(w))
    raise AssertionError(f"stage {stage} never dispatched")


def test_implement_prompt_contains_scope_plan(tmp_path, project) -> None:
    # review Phase B acceptance check: scope's plan reaches implement's prompt
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    prompt = _prompt_at(eng, Stage.IMPLEMENT)
    assert "subtask-1" in prompt  # the plan scope produced (_default_output SCOPE)
    assert "Context from earlier stages" in prompt


def test_workitem_cwd_is_the_folded_worktree(tmp_path, project) -> None:
    # review Phase B #7: a WorkItem runs in the task's worktree, not process CWD
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    intake = eng.next_work("r1", "t1")
    assert intake.stage is Stage.INTAKE
    assert intake.cwd is None  # intake creates the worktree; nothing folded yet
    eng.record("r1", make_result(intake))  # folds worktree=/wt/42 into context
    scope = eng.next_work("r1", "t1")
    assert scope.cwd == "/wt/42"  # every later stage runs in the worktree


def test_review_prompt_points_at_the_diff_range_not_a_pr(tmp_path, project) -> None:
    """#389: REVIEW runs BEFORE the PR is opened, so it is aimed at a commit range.

    The acceptance criterion the split has to keep: the reviewer can still see exactly
    this task's change. It gets that from ``base_sha`` (folded at INTAKE, the same range
    TEST diffs) rather than from a pr_url — and no pr_url can be in its context, because
    at REVIEW time no PR exists for this task at all."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    prompt = _prompt_at(eng, Stage.REVIEW)
    assert "base_sha" in prompt  # the range the reviewer reads
    assert "pr_url" not in prompt and "/1234" not in prompt  # nothing to go stale


def test_publish_prompt_follows_an_approved_review(tmp_path, project) -> None:
    """#389: PUBLISH is the stage that has a PR to talk about, and it comes last."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    prompt = _prompt_at(eng, Stage.PUBLISH)
    assert "PUBLISH" in prompt and "pull request" in prompt


def test_prompt_orders_stable_parts_before_per_task_context(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    prompt = _prompt_at(eng, Stage.IMPLEMENT)
    # stable project commands + task spec precede the growing per-task context block
    i_cmds = prompt.index("## Project commands")
    i_task = prompt.index("## Task t1")
    i_ctx = prompt.index("## Context from earlier stages")
    assert i_cmds < i_task < i_ctx
    # the decorative-no-more ProjectConfig commands are actually in the prompt now
    assert "install" in prompt and "typecheck" in prompt


# --- helpers ---------------------------------------------------------------
def make_result_stub(stage: Stage, output: dict, *, mode=None, provider=None):
    from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
    from orchestrator.schemas.work import LaneUsed, StageResult, TokenUsage

    return StageResult(
        work_item_id="wi", content_hash="h", run_id="r", task_id="t", stage=stage,
        attempt=0, model="claude-opus-5", status=ResultStatus.SUCCESS,
        structured_output=output,
        lane_used=LaneUsed(execution_mode=mode or ExecutionMode.INTERACTIVE,
                           provider=provider or Provider.CLAUDE, invocation="x"),
        token_usage=TokenUsage(), completed_at="2026-07-01T00:00:00Z",
    )


# --- #41: the change_class fold is deterministic-only (no model loophole) --------------
def test_change_class_folds_only_from_the_engine_lane() -> None:
    from orchestrator.schemas.enums import ExecutionMode, Provider
    from orchestrator.schemas.status import Task
    from orchestrator.state_machine import DETERMINISTIC_ONLY_KEYS

    assert "change_class" in DETERMINISTIC_ONLY_KEYS
    assert "change_class" in CONTEXT_KEYS[Stage.TEST]

    # A MODEL-lane (interactive) TEST result claiming docs-only is IGNORED — a model must
    # not be able to relax downstream gates by asserting the tag.
    model_task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    _absorb_outputs(model_task, make_result_stub(
        Stage.TEST,
        {"passed": True, "failures": [], "tests_meaningful": True, "change_class": "docs-only"},
    ))
    assert "change_class" not in model_task.context

    # The SAME output on the deterministic ENGINE lane DOES fold — only a git-diff sets it.
    engine_task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    _absorb_outputs(engine_task, make_result_stub(
        Stage.TEST,
        {"passed": True, "failures": [], "tests_meaningful": True, "change_class": "docs-only"},
        mode=ExecutionMode.ENGINE, provider=Provider.NONE,
    ))
    assert engine_task.context["change_class"] == "docs-only"
