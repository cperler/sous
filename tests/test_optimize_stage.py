"""The optional OPTIMIZE stage (#519): a measured speed pass, for a project where speed is
part of the product.

The whole opt-in is one roster entry. A project whose ``agent_for`` answers nothing for the
``optimize`` role must run byte-identically to a pre-#519 engine, so most of what these tests
assert is an ABSENCE — the stage missing from the pipeline, the dispatch sequence unchanged,
no extra paid stage anybody asked for.

The other half is the architectural suggestions the stage deliberately does NOT apply. Those
are the reason the stage can stay bounded: "don't restructure the system here" only works if
the idea survives, so the engine files the `file`-dispositioned ones as enhancement issues on
the same cap-and-dedupe terms as a review finding, and the completion note carries the rest.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import (
    LANE_STAGES,
    ExecutionLane,
    QualityTier,
    Stage,
    StageStatus,
    with_optimize,
)
from orchestrator.schemas.stage_schemas import resolve_stage_schema
from orchestrator.stages import STAGE_SPECS
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result

CHECKPOINT = {"tag": "task/r1/t1/optimize/0", "sha": "a" * 40}


class OptimizingProject(FakeProject):
    """A project that opts in: its roster names an agent for the ``optimize`` role."""

    def agent_for(self, stage: Stage, role: str | None = None):
        return (
            {"implement": "impl-agent", "review": "code-reviewer",
             "docstring": "docstring-agent", "optimize": "performance-optimizer"}
        ).get(role)


@pytest.fixture
def optimizing() -> OptimizingProject:
    return OptimizingProject()


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "cost.jsonl"), project, **kw)


def _events(tmp_path) -> list[dict]:
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _optimize_output(suggestions: list[dict] | None = None, **kw) -> dict:
    return {
        "files_changed": ["hot.py"],
        "summary": "batched the inner loop and narrowed the lock",
        "committed": True,
        "measurements": [{"name": "score_batch", "before": 420.0, "after": 96.0, "unit": "ms"}],
        "suggestions": suggestions if suggestions is not None else [],
        **kw,
    }


def _drive(eng: Engine, *, run="r1", task="t1", outputs: dict | None = None) -> list:
    """Supervisor loop to completion, injecting per-stage structured output overrides."""
    outcomes = []
    while (work := eng.next_work(run, task)) is not None:
        outcomes.append(
            eng.record(
                run,
                make_result(
                    work,
                    structured_output=(outputs or {}).get(work.stage),
                    checkpoint=CHECKPOINT if work.stage is Stage.OPTIMIZE else None,
                ),
            )
        )
    return outcomes


# --- the opt-in: one roster entry, and nothing else changes --------------------------


def test_without_a_roster_entry_the_stage_never_enters_the_pipeline(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    task = eng.add_task("r1", "t1")

    assert Stage.OPTIMIZE not in task.pipeline
    assert task.pipeline == LANE_STAGES[ExecutionLane.FULL]
    # Off-pipeline stages are marked skipped, so the stage is visibly declined rather than
    # silently absent from the record — but it never dispatches.
    dispatched = []
    while (work := eng.next_work("r1", "t1")) is not None:
        dispatched.append(work.stage)
        eng.record("r1", make_result(work))
    assert Stage.OPTIMIZE not in dispatched
    assert dispatched == list(LANE_STAGES[ExecutionLane.FULL])
    assert eng.store.load_task("r1", "t1").stages[Stage.OPTIMIZE].status is StageStatus.SKIPPED
    assert not any(e["type"] == "pipeline_stage_inserted" for e in _events(tmp_path))


@pytest.mark.parametrize("lane", list(ExecutionLane))
def test_no_lane_preset_carries_optimize(lane) -> None:
    """The stage is vocabulary, not a preset step — the same shape SIMPLIFY has."""
    assert Stage.OPTIMIZE not in LANE_STAGES[lane]


def test_the_roster_entry_inserts_the_stage_after_simplify(tmp_path, optimizing) -> None:
    eng = _engine(tmp_path, optimizing)
    eng.create_run("r1", ExecutionLane.FULL)
    pinned = [Stage.INTAKE, Stage.IMPLEMENT, Stage.SIMPLIFY, Stage.TEST, Stage.DELIVER]
    task = eng.add_task("r1", "t1", pipeline=pinned)

    assert task.pipeline == (
        Stage.INTAKE, Stage.IMPLEMENT, Stage.SIMPLIFY, Stage.OPTIMIZE, Stage.TEST, Stage.DELIVER
    )
    inserted = next(e for e in _events(tmp_path) if e["type"] == "pipeline_stage_inserted")
    assert inserted["stage"] == "optimize"
    assert inserted["agent"] == "performance-optimizer"


def test_without_a_simplify_pass_it_follows_implement(tmp_path, optimizing) -> None:
    eng = _engine(tmp_path, optimizing)
    eng.create_run("r1", ExecutionLane.FULL)
    task = eng.add_task("r1", "t1")

    # The change must exist before it can be measured, and TEST must still run after.
    assert task.pipeline.index(Stage.OPTIMIZE) == task.pipeline.index(Stage.IMPLEMENT) + 1
    assert task.pipeline.index(Stage.OPTIMIZE) < task.pipeline.index(Stage.TEST)


def test_a_pipeline_that_writes_no_code_is_left_alone() -> None:
    """Nothing to optimize, so no stage — a pure helper, so assert it directly."""
    review_only = (Stage.INTAKE, Stage.REVIEW)
    assert with_optimize(review_only) == review_only
    # Idempotent: a pin that already names the stage keeps its own ordering.
    named = (Stage.INTAKE, Stage.OPTIMIZE, Stage.IMPLEMENT)
    assert with_optimize(named) == named


def test_an_adapter_that_raises_on_the_roster_probe_is_not_opted_in(tmp_path, project) -> None:
    """Registration must survive an adapter with no opinion about optimizing."""

    def boom(stage, role=None):
        raise RuntimeError("no roster here")

    project.agent_for = boom
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    assert Stage.OPTIMIZE not in eng.add_task("r1", "t1").pipeline


# --- the stage itself ---------------------------------------------------------------


def test_the_stage_dispatches_with_its_spec_and_checkpoints(tmp_path, optimizing) -> None:
    eng = _engine(tmp_path, optimizing)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    while (work := eng.next_work("r1", "t1")).stage is not Stage.OPTIMIZE:
        eng.record("r1", make_result(work))

    spec = STAGE_SPECS[Stage.OPTIMIZE]
    assert work.agent == "performance-optimizer"  # the roster entry that opted in
    assert work.schema_ref == "optimize"
    assert work.timeout_s == spec.timeout_s and work.effort == spec.effort
    assert work.checkpoint_tag  # a committing stage: a retry resets to the last-good tag
    # #317: every committing stage is told not to sign its commits, off the spec's own flag.
    assert "Co-Authored-By" in work.prompt

    eng.record("r1", make_result(work, structured_output=_optimize_output(),
                                 checkpoint=CHECKPOINT))
    task = eng.store.load_task("r1", "t1")
    assert task.stages[Stage.OPTIMIZE].status is StageStatus.COMPLETED
    # #520: absorbing the checkpoint is now CONDITIONAL on the engine's own benchmark. This
    # project declares no benchmark_cmd, so the commit is unverified and therefore not kept —
    # tests/test_optimize_benchmark_gate.py owns the verified path.
    assert task.last_checkpoint is None
    assert task.stages[Stage.OPTIMIZE].output["benchmark"]["status"] == "unavailable"


def test_the_resolved_pipeline_survives_a_rebuilt_engine(tmp_path, optimizing) -> None:
    """The run-settings persistence norm: the decision lives on the Task doc, not in engine
    memory, so the next CLI subcommand — a fresh Engine from constructor defaults, and here
    one whose project has since LOST the roster entry — still sequences the stage it planned.
    """
    _engine(tmp_path, optimizing).create_run("r1", ExecutionLane.FULL)
    _engine(tmp_path, optimizing).add_task("r1", "t1")

    later = _engine(tmp_path, FakeProject())  # no 'optimize' role at all
    assert Stage.OPTIMIZE in later.store.load_task("r1", "t1").pipeline
    stages = []
    while (work := later.next_work("r1", "t1")) is not None:
        stages.append(work.stage)
        later.record("r1", make_result(work))
    assert Stage.OPTIMIZE in stages


def test_a_none_quality_tier_child_declines_the_stage(tmp_path, optimizing) -> None:
    """`none` means run no quality pass, and a speed pass is one."""
    eng = _engine(tmp_path, optimizing)
    eng.create_run("r1", ExecutionLane.FULL)

    full = eng.add_task("r1", "child-full", quality_tier=QualityTier.FULL)
    bare = eng.add_task("r1", "child-none", quality_tier=QualityTier.NONE)

    assert Stage.OPTIMIZE in full.pipeline
    assert Stage.OPTIMIZE not in bare.pipeline


def test_the_stage_output_contract_validates(tmp_path) -> None:
    schema = resolve_stage_schema("optimize")
    jsonschema.validate(_optimize_output(), schema)
    jsonschema.validate(
        _optimize_output([{"description": "Move scoring to a process pool",
                           "rationale": "the GIL bounds it", "disposition": "file"}]),
        schema,
    )
    # measurements is required: an unmeasured speed claim is not a speed claim (an empty
    # array is the honest answer for a no-op pass).
    bare = _optimize_output()
    bare.pop("measurements")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bare, schema)


# --- the suggestions the stage did not apply ----------------------------------------


_SUGGESTIONS = [
    {"description": "Move scoring to a process pool",
     "rationale": "the GIL bounds the hot path; threads cannot help", "disposition": "file"},
    {"description": "Share the model weights through shared memory",
     "rationale": "each worker currently copies 400MB", "disposition": "file"},
    {"description": "Rewrite the whole scheduler in Rust",
     "rationale": "would be fast", "disposition": "drop"},
    {"description": "Maybe look at the cache layout", "rationale": "unclear"},
]


def _drive_with_suggestions(tmp_path, project, suggestions, **engine_kw) -> Engine:
    eng = _engine(tmp_path, project, **engine_kw)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _drive(eng, outputs={Stage.OPTIMIZE: _optimize_output(suggestions)})
    return eng


def test_file_disposition_suggestions_become_enhancement_issues(tmp_path, optimizing) -> None:
    _drive_with_suggestions(tmp_path, optimizing, _SUGGESTIONS)

    ts = optimizing.task_source
    filed = [f for f in ts.followups if f["labels"] == ["enhancement"]]
    assert [f["title"] for f in filed] == [
        "Move scoring to a process pool",
        "Share the model weights through shared memory",
    ]
    # The body carries the rationale and names the task it came from (triage-followups
    # matches on that footer, not on a label).
    assert "the GIL bounds the hot path" in filed[0]["body"]
    assert "t1" in filed[0]["body"] and "OPTIMIZE" in filed[0]["body"]

    events = _events(tmp_path)
    assert sum(e["type"] == "optimize_suggestion_filed" for e in events) == 2
    # `drop` and no-disposition are evented as declined, never silently gone.
    declined = [e for e in events if e["type"] == "optimize_suggestion_not_filed"]
    assert {e["title"] for e in declined} == {
        "Rewrite the whole scheduler in Rust", "Maybe look at the cache layout"
    }
    completed = next(e for e in events if e["type"] == "task_completed")
    assert completed["optimize_suggestions_filed"] == 2


def test_the_per_task_cap_bounds_the_filing(tmp_path, optimizing) -> None:
    eng = _engine(tmp_path, optimizing)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", max_filed_followups=1)
    _drive(eng, outputs={Stage.OPTIMIZE: _optimize_output(_SUGGESTIONS)})

    filed = [f for f in optimizing.task_source.followups if f["labels"] == ["enhancement"]]
    assert [f["title"] for f in filed] == ["Move scoring to a process pool"]
    over = next(
        e for e in _events(tmp_path)
        if e["type"] == "optimize_suggestion_not_filed"
        and e["title"] == "Share the model weights through shared memory"
    )
    assert over["reason"] == "over per-task cap"
    # Over-cap is noted for a human, not dropped.
    assert "over per-task cap" in optimizing.task_source.notes[0]["body"]


def test_the_run_wide_cap_applies_when_the_task_pins_none(tmp_path, optimizing) -> None:
    """Same precedence the review follow-ups use: task > run > engine default."""
    eng = _engine(tmp_path, optimizing)
    eng.create_run("r1", ExecutionLane.FULL, max_filed_followups=0)
    eng.add_task("r1", "t1")
    _drive(eng, outputs={Stage.OPTIMIZE: _optimize_output(_SUGGESTIONS)})

    assert not [f for f in optimizing.task_source.followups if f["labels"] == ["enhancement"]]


def test_a_suggestion_matching_a_filed_finding_is_deduped(tmp_path, optimizing) -> None:
    """One observation, one issue: the reviewer and the optimizer read the same diff."""
    eng = _engine(tmp_path, optimizing)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _drive(eng, outputs={
        Stage.REVIEW: {
            "approved": True, "issues": [],
            "non_blocking": [{"title": "Move scoring to a process pool",
                              "detail": "the GIL bounds it", "disposition": "file"}],
        },
        Stage.OPTIMIZE: _optimize_output(_SUGGESTIONS[:1]),
    })

    titles = [f["title"] for f in optimizing.task_source.followups]
    assert titles == ["Move scoring to a process pool"]  # filed once, by the review path
    assert any(e["type"] == "optimize_suggestion_deduped" for e in _events(tmp_path))


def test_a_suggestion_matching_the_filed_improvement_is_deduped(tmp_path, optimizing) -> None:
    eng = _engine(tmp_path, optimizing)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _drive(eng, outputs={
        Stage.REVIEW: {
            "approved": True, "issues": [],
            "improvement": {"title": "Move scoring to a process pool",
                            "detail": "the GIL bounds it", "disposition": "file"},
        },
        Stage.OPTIMIZE: _optimize_output(_SUGGESTIONS[:1]),
    })

    assert [f["title"] for f in optimizing.task_source.followups] == [
        "Move scoring to a process pool"
    ]
    assert any(e["type"] == "optimize_suggestion_deduped" for e in _events(tmp_path))


def test_a_deduped_suggestion_is_not_reported_as_cap_overflow(tmp_path, optimizing) -> None:
    """A suppressed duplicate already HAS an issue. Reporting it as cap overflow would tell a
    human to raise the cap and file it again — the one-idea-filed-twice failure the dedupe
    exists to prevent, arriving through the note instead of the tracker.
    """
    eng = _engine(tmp_path, optimizing)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _drive(eng, outputs={
        Stage.REVIEW: {
            "approved": True, "issues": [],
            "non_blocking": [{"title": "Move scoring to a process pool",
                              "detail": "the GIL bounds it", "disposition": "file"}],
        },
        # The duplicate first, then a second `file` suggestion: the duplicate must not consume
        # the cap budget either, or suppressing it would silently cost the next idea its issue.
        Stage.OPTIMIZE: _optimize_output(_SUGGESTIONS[:2]),
    })

    note = optimizing.task_source.notes[0]["body"]
    assert "Move scoring to a process pool — already filed above" in note
    assert "Move scoring to a process pool — over per-task cap" not in note
    filed = [f["title"] for f in optimizing.task_source.followups]
    assert filed == ["Move scoring to a process pool",
                     "Share the model weights through shared memory"]
    completed = next(e for e in _events(tmp_path) if e["type"] == "task_completed")
    assert completed["optimize_suggestions_filed"] == 1  # the dedupe is not a filing


def test_a_duplicate_is_deduped_even_when_an_earlier_suggestion_filled_the_cap(
    tmp_path, optimizing
) -> None:
    """Order must not decide what a suppressed duplicate is CALLED.

    The sibling test above files the duplicate first, so the cap is still open when it is
    reached. Here the novel suggestion goes first and exhausts a cap of 1, so a cap-first
    check would report the duplicate as "over per-task cap" — telling a human to raise the
    cap and file a second copy of an issue the review already filed.
    """
    eng = _engine(tmp_path, optimizing)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", max_filed_followups=1)
    _drive(eng, outputs={
        Stage.REVIEW: {
            "approved": True, "issues": [],
            "non_blocking": [{"title": "Move scoring to a process pool",
                              "detail": "the GIL bounds it", "disposition": "file"}],
        },
        # novel first (consumes the whole cap), duplicate second
        Stage.OPTIMIZE: _optimize_output([_SUGGESTIONS[1], _SUGGESTIONS[0]]),
    })

    events = _events(tmp_path)
    assert any(
        e["type"] == "optimize_suggestion_deduped"
        and e["title"] == "Move scoring to a process pool"
        for e in events
    )
    assert not any(
        e["type"] == "optimize_suggestion_not_filed"
        and e["title"] == "Move scoring to a process pool"
        for e in events
    )
    note = optimizing.task_source.notes[0]["body"]
    assert "Move scoring to a process pool — already filed above" in note
    assert "Move scoring to a process pool — over per-task cap" not in note
    # The cap still bounds real filings: the review's one, plus the one novel suggestion.
    assert [f["title"] for f in optimizing.task_source.followups] == [
        "Move scoring to a process pool",
        "Share the model weights through shared memory",
    ]


def test_the_completion_note_carries_measurements_and_both_suggestion_halves(
    tmp_path, optimizing
) -> None:
    _drive_with_suggestions(tmp_path, optimizing, _SUGGESTIONS)

    note = optimizing.task_source.notes[0]["body"]
    assert "score_batch: 420.0 ms → 96.0 ms" in note
    assert "### Architectural suggestions (not applied here)" in note
    # filed ones link their issue; declined ones state why instead
    assert "Move scoring to a process pool → https://example.test/issues/" in note
    assert "Rewrite the whole scheduler in Rust — noted, not tracked" in note
    assert "Maybe look at the cache layout — no disposition given — not filed" in note


def test_a_flaky_task_source_cannot_break_finalize(tmp_path, optimizing) -> None:
    source = optimizing.task_source

    def boom(title: str, body: str, labels=None) -> str:
        raise RuntimeError("gh is down")

    source.file_followup = boom
    eng = _engine(tmp_path, optimizing)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    outcomes = _drive(eng, outputs={Stage.OPTIMIZE: _optimize_output(_SUGGESTIONS)})

    assert outcomes[-1]["outcome"] == "task_completed"
    failures = [e for e in _events(tmp_path) if e["type"] == "optimize_suggestion_failed"]
    assert len(failures) == 2 and "gh is down" in failures[0]["error"]


def test_finalize_survives_a_malformed_suggestions_payload(tmp_path, optimizing) -> None:
    """The interactive lane validates nothing, so the payload may be any shape."""
    eng = _engine(tmp_path, optimizing)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    outcomes = _drive(eng, outputs={
        Stage.OPTIMIZE: _optimize_output(
            ["a bare string", {"description": 17, "disposition": "file"},
             {"rationale": "no description at all", "disposition": "file"}]
        ),
    })

    assert outcomes[-1]["outcome"] == "task_completed"
    # The int description coerces to a usable title rather than crashing or filing nothing.
    assert [f["title"] for f in optimizing.task_source.followups] == ["17"]


def test_a_long_description_is_bounded_into_a_title(tmp_path, optimizing) -> None:
    description = "Restructure the worker pool " + "and the queue " * 40
    _drive_with_suggestions(
        tmp_path, optimizing,
        [{"description": description, "rationale": "r", "disposition": "file"}],
    )

    title = optimizing.task_source.followups[0]["title"]
    assert len(title) <= 200 and title.endswith("…")
    assert title.startswith("Restructure the worker pool")
