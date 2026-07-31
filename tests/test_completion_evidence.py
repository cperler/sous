"""Evidence-out at task finalize (the 'publish the reasoning' half of the seam).

When a task completes the engine files explicitly opted-in non-blocking review findings
as deferred-scope follow-ups and posts a completion note — both via OPTIONAL duck-typed
task-source hooks, so an adapter without them is a graceful no-op and a flaky hook can
never un-complete a finished task.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from adapters.project.base import TaskSpec
from adapters.project.github_issues import GitHubIssuesSource
from adapters.project.selfhost.task_source import LocalFileTaskSource
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.render import render_completion_note
from orchestrator.schemas.enums import ExecutionLane, Stage, StageStatus
from orchestrator.schemas.stage_schemas import resolve_stage_schema
from orchestrator.schemas.status import Task
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    store = StatusStore(tmp_path)
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    return Engine(store, ledger, project, **kw)


def _drive(eng: Engine, *, review_output: dict, run="r1", task="t1") -> list:
    """Supervisor loop to completion, injecting a custom review structured_output."""
    outcomes = []
    while (work := eng.next_work(run, task)) is not None:
        so = review_output if work.stage is Stage.REVIEW else None
        outcomes.append(eng.record(run, make_result(work, structured_output=so)))
    return outcomes


_REVIEW_WITH_FINDINGS = {
    "approved": True,
    "issues": [],
    "non_blocking": [
        {"title": "Handle a nonexistent --repo path", "detail": "raises FileNotFoundError",
         "disposition": "file"},
        {"title": "Guard negative --keep-latest", "detail": "a negative N is nonsensical",
         "disposition": "file"},
    ],
}


def _events(tmp_path) -> list[dict]:
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- engine finalize ---------------------------------------------------------------


def test_finalize_files_followups_and_publishes_note(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    outcomes = _drive(eng, review_output=_REVIEW_WITH_FINDINGS)
    assert outcomes[-1]["outcome"] == "task_completed"

    ts = project.task_source
    # one follow-up per non-blocking finding, all labeled deferred-scope
    assert [f["title"] for f in ts.followups] == [
        "Handle a nonexistent --repo path",
        "Guard negative --keep-latest",
    ]
    assert all(f["labels"] == ["deferred-scope"] for f in ts.followups)
    assert "#10" not in ts.followups[0]["body"]  # (uses the real task id t1, not a literal)
    assert "t1" in ts.followups[0]["body"]  # provenance back-reference

    # exactly one completion note, on the PR, reflecting the verdict + follow-ups
    assert len(ts.notes) == 1
    note = ts.notes[0]
    assert note["pr_url"].endswith("/1234")
    assert "✅ approved" in note["body"]
    assert "Follow-ups filed" in note["body"]
    assert note["body"].count("https://example.test/issues/") == 2

    events = _events(tmp_path)
    assert sum(e["type"] == "followup_filed" for e in events) == 2
    completed = next(e for e in events if e["type"] == "task_completed")
    assert completed["followups_filed"] == 2


_REVIEW_WITH_SELF_IMPROVEMENT = {
    "approved": True,
    "issues": [],
    "improvement": {"title": "Add a schema-validate-retry loop to the transport",
                    "detail": "Retire the postamble band-aid with a real retry.",
                    "disposition": "file"},
    "retrospective": {"title": "Terse stage prompts under-elicit structured output",
                      "detail": "Tune prompts against real run transcripts."},
}


def test_finalize_files_improvement_and_renders_self_improvement(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    _drive(eng, review_output=_REVIEW_WITH_SELF_IMPROVEMENT)

    ts = project.task_source
    # the improvement idea is filed as an ENHANCEMENT issue (not deferred-scope)
    enh = [f for f in ts.followups if f["labels"] == ["enhancement"]]
    assert len(enh) == 1
    assert enh[0]["title"] == "Add a schema-validate-retry loop to the transport"

    note = ts.notes[0]["body"]
    assert "### Self-improvement" in note
    assert "Improvement idea" in note and enh[0]["ref"] in note  # links the filed issue
    assert "Process retrospective" in note and "under-elicit structured output" in note

    events = _events(tmp_path)
    assert any(e["type"] == "improvement_filed" for e in events)
    completed = next(e for e in events if e["type"] == "task_completed")
    assert completed["improvement_filed"] is True


def test_finalize_survives_a_non_string_improvement_field(tmp_path, project) -> None:
    # The interactive lane doesn't schema-validate structured_output, so a model may emit
    # a non-string field. Evidence-out must NOT crash record()/finalize.
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    outcomes = _drive(eng, review_output={
        "approved": True, "issues": [],
        "improvement": {"title": "coerce me", "detail": ["a", "list", "not", "a", "string"]},
    })

    assert outcomes[-1]["outcome"] == "task_completed"  # record() did not crash
    events = _events(tmp_path)
    assert any(e["type"] == "task_completed" for e in events)  # finalize event still fired
    assert not any(e["type"] == "evidence_out_failed" for e in events)  # coerced, not crashed
    assert len(project.task_source.notes) == 1  # the note still published


def test_finalize_no_findings_files_nothing_but_still_notes(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    _drive(eng, review_output={"approved": True, "issues": []})

    ts = project.task_source
    assert ts.followups == []
    assert len(ts.notes) == 1  # a clean run still publishes its completion evidence


# --- #188 filing threshold (disposition gate / per-task cap / dedup) ----------------


def test_disposition_gate_files_only_explicit_file(tmp_path, project) -> None:
    # Filing is opt-in; every non-file value is surfaced in the completion note so neither
    # a dispositionless finding nor a dispositionless improvement disappears silently.
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    _drive(eng, review_output={
        "approved": True, "issues": [],
        "non_blocking": [
            {"title": "Automate the events-balance audit", "detail": "d", "disposition": "file"},
            {"title": "Finding without a disposition", "detail": "d"},
            {"title": "Finding with an empty disposition", "detail": "d", "disposition": ""},
            {"title": "Finding with an unknown disposition", "detail": "d",
             "disposition": "maybe"},
            {"title": "Stale docstring after #133", "detail": "d", "disposition": "fix_now"},
            {"title": "Cosmetic wording nit", "detail": "d", "disposition": "drop"},
        ],
        "improvement": {"title": "Dispositionless improvement", "detail": "d"},
    })

    ts = project.task_source
    filed_titles = [f["title"] for f in ts.followups]
    assert filed_titles == ["Automate the events-balance audit"]
    # all unfiled findings and the improvement are durable in the note, with their reason
    note = ts.notes[0]["body"]
    assert "### Noted, not filed" in note
    assert "Finding without a disposition — no disposition given — not filed" in note
    assert "Finding with an empty disposition — no disposition given — not filed" in note
    assert "Finding with an unknown disposition — unrecognized disposition — not filed" in note
    assert "Stale docstring after #133 — fixed in place (boy-scout)" in note
    assert "Cosmetic wording nit — noted, not tracked" in note
    assert "Dispositionless improvement — no disposition given — not filed" in note

    events = _events(tmp_path)
    not_filed = [e for e in events if e["type"] == "improvement_not_filed"]
    assert len(not_filed) == 1 and not_filed[0]["disposition"] is None


def test_per_task_cap_bounds_filed_followups(tmp_path, project) -> None:
    # More than the cap of `file` findings: only the first N are filed; the overflow is
    # noted, not dropped.
    from orchestrator.engine import MAX_FILED_FOLLOWUPS_PER_TASK

    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    findings = [
        {"title": f"File-worthy finding {i}", "detail": "d", "disposition": "file"}
        for i in range(MAX_FILED_FOLLOWUPS_PER_TASK + 2)
    ]
    _drive(eng, review_output={"approved": True, "issues": [], "non_blocking": findings})

    ts = project.task_source
    assert len(ts.followups) == MAX_FILED_FOLLOWUPS_PER_TASK
    # the overflow findings are surfaced as "over per-task cap", not silently dropped
    note = ts.notes[0]["body"]
    assert "### Noted, not filed" in note
    assert note.count("over per-task cap") == 2


def test_per_task_cap_override_raises_the_limit(tmp_path, project) -> None:
    # #191: a task type with a larger review surface files MORE follow-ups than the engine
    # default, set per task at add_task without touching engine code.
    from orchestrator.engine import MAX_FILED_FOLLOWUPS_PER_TASK

    raised = MAX_FILED_FOLLOWUPS_PER_TASK + 2
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", max_filed_followups=raised)

    findings = [
        {"title": f"File-worthy finding {i}", "detail": "d", "disposition": "file"}
        for i in range(raised + 1)
    ]
    _drive(eng, review_output={"approved": True, "issues": [], "non_blocking": findings})

    ts = project.task_source
    # the per-task cap (not the default) bounds the filing; the single overflow is noted
    assert len(ts.followups) == raised
    assert ts.notes[0]["body"].count("over per-task cap") == 1


def test_per_task_cap_override_of_zero_files_nothing(tmp_path, project) -> None:
    # #191: a cap of 0 files no follow-ups at all — every `file` finding is noted, not filed.
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", max_filed_followups=0)

    findings = [
        {"title": f"File-worthy finding {i}", "detail": "d", "disposition": "file"}
        for i in range(3)
    ]
    _drive(eng, review_output={"approved": True, "issues": [], "non_blocking": findings})

    ts = project.task_source
    assert ts.followups == []
    assert ts.notes[0]["body"].count("over per-task cap") == 3


def test_engine_default_cap_is_configurable(tmp_path, project) -> None:
    # #191: the engine-wide default (run-create time) is tunable, and an un-overridden task
    # inherits it.
    eng = _engine(tmp_path, project, max_filed_followups=1)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")  # no per-task override -> inherits the engine default of 1

    findings = [
        {"title": f"File-worthy finding {i}", "detail": "d", "disposition": "file"}
        for i in range(3)
    ]
    _drive(eng, review_output={"approved": True, "issues": [], "non_blocking": findings})

    ts = project.task_source
    assert len(ts.followups) == 1
    assert ts.notes[0]["body"].count("over per-task cap") == 2


def test_negative_per_task_cap_is_rejected(tmp_path, project) -> None:
    # #191: a negative cap is nonsensical (0 already means "file nothing") — rejected before
    # any state is written.
    from orchestrator.errors import ContractError

    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    with pytest.raises(ContractError, match="max_filed_followups must be >= 0"):
        eng.add_task("r1", "t1", max_filed_followups=-1)


def test_run_default_cap_bounds_filed_followups(tmp_path, project) -> None:
    # #196: the run-wide default (set at create_run) caps an un-overridden task, so every task
    # in the run shares the baseline without repeating it per add_task.
    eng = _engine(tmp_path, project)  # engine constructor default (2) is NOT what binds here
    eng.create_run("r1", ExecutionLane.FULL, max_filed_followups=1)
    eng.add_task("r1", "t1")  # no per-task override -> inherits the run default of 1

    findings = [
        {"title": f"File-worthy finding {i}", "detail": "d", "disposition": "file"}
        for i in range(3)
    ]
    _drive(eng, review_output={"approved": True, "issues": [], "non_blocking": findings})

    ts = project.task_source
    assert len(ts.followups) == 1
    assert ts.notes[0]["body"].count("over per-task cap") == 2


def test_per_task_cap_overrides_run_default(tmp_path, project) -> None:
    # #196: the per-task cap still wins over the run-wide default (task > run > engine).
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL, max_filed_followups=0)  # run says file nothing
    eng.add_task("r1", "t1", max_filed_followups=2)  # ...but this task overrides to 2

    findings = [
        {"title": f"File-worthy finding {i}", "detail": "d", "disposition": "file"}
        for i in range(3)
    ]
    _drive(eng, review_output={"approved": True, "issues": [], "non_blocking": findings})

    ts = project.task_source
    assert len(ts.followups) == 2
    assert ts.notes[0]["body"].count("over per-task cap") == 1


def test_negative_run_cap_is_rejected(tmp_path, project) -> None:
    # #196: a negative run-wide cap is rejected before the run is written, like the per-task one.
    from orchestrator.errors import ContractError

    eng = _engine(tmp_path, project)
    with pytest.raises(ContractError, match="max_filed_followups must be >= 0"):
        eng.create_run("r1", ExecutionLane.FULL, max_filed_followups=-1)


def test_improvement_deduped_against_filed_followup(tmp_path, project) -> None:
    # The #186/#187 class: one observation emitted as both a non_blocking finding and the
    # improvement idea must be filed ONCE, not twice.
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    shared = "Type abandon() disposition as a Literal"
    _drive(eng, review_output={
        "approved": True, "issues": [],
        "non_blocking": [{"title": shared, "detail": "d", "disposition": "file"}],
        "improvement": {"title": shared, "detail": "same idea, restated",
                        "disposition": "file"},
    })

    ts = project.task_source
    # filed exactly once (as the deferred-scope follow-up), not also as an enhancement
    assert len(ts.followups) == 1
    assert ts.followups[0]["labels"] == ["deferred-scope"]
    assert not any(f["labels"] == ["enhancement"] for f in ts.followups)

    events = _events(tmp_path)
    assert any(e["type"] == "improvement_deduped" for e in events)
    completed = next(e for e in events if e["type"] == "task_completed")
    assert completed["improvement_filed"] is False


def test_distinct_improvement_still_files(tmp_path, project) -> None:
    # A genuinely distinct improvement (no fingerprint overlap) is still filed as an
    # enhancement — dedup must not swallow real ideas.
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    _drive(eng, review_output={
        "approved": True, "issues": [],
        "non_blocking": [{"title": "Guard the --repo path", "detail": "d", "disposition": "file"}],
        "improvement": {"title": "Add a run-finalize dedup pass", "detail": "different idea",
                        "disposition": "file"},
    })

    ts = project.task_source
    assert [f["labels"] for f in ts.followups] == [["deferred-scope"], ["enhancement"]]


@pytest.mark.parametrize("disposition", ["fix_now", "drop", "", "maybe"])
def test_improvement_disposition_gate_suppresses_filing(tmp_path, project, disposition) -> None:
    # A recognized non-file, empty, or unrecognized disposition is NOT filed as an
    # enhancement — but it IS surfaced in the completion note (nothing silently dropped).
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    _drive(eng, review_output={
        "approved": True, "issues": [],
        "improvement": {"title": "Expose a retry-count knob",
                        "detail": "speculative tunability", "disposition": disposition},
    })

    ts = project.task_source
    # no enhancement issue filed for the suppressed improvement
    assert not any(f["labels"] == ["enhancement"] for f in ts.followups)

    # ...but it is durable in the completion note, with a why-not-filed reason
    note = ts.notes[0]["body"]
    assert "Expose a retry-count knob" in note
    reason = {
        "fix_now": "noted for in-place handling, not filed",
        "drop": "noted, not tracked",
        "": "no disposition given — not filed",
        "maybe": "unrecognized disposition — not filed",
    }[disposition]
    assert reason in note

    events = _events(tmp_path)
    not_filed = [e for e in events if e["type"] == "improvement_not_filed"]
    assert len(not_filed) == 1 and not_filed[0]["disposition"] == disposition
    assert not any(e["type"] == "improvement_filed" for e in events)
    completed = next(e for e in events if e["type"] == "task_completed")
    assert completed["improvement_filed"] is False


def test_improvement_with_explicit_file_disposition_still_files(tmp_path, project) -> None:
    # The allowlist gate still files an explicitly opted-in improvement as an enhancement.
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    improvement = {"title": "Add a schema-validate-retry loop", "detail": "real need",
                   "disposition": "file"}
    _drive(eng, review_output={"approved": True, "issues": [], "improvement": improvement})

    ts = project.task_source
    enh = [f for f in ts.followups if f["labels"] == ["enhancement"]]
    assert len(enh) == 1
    assert enh[0]["title"] == "Add a schema-validate-retry loop"

    events = _events(tmp_path)
    assert any(e["type"] == "improvement_filed" for e in events)
    assert not any(e["type"] == "improvement_not_filed" for e in events)
    completed = next(e for e in events if e["type"] == "task_completed")
    assert completed["improvement_filed"] is True


class _FailFirstSource:
    """file_followup raises on the FIRST call then succeeds — a transient filing failure."""

    def __init__(self) -> None:
        self.followups: list = []
        self.notes: list = []
        self._calls = 0

    def resolve(self, task_id: str) -> TaskSpec:
        return TaskSpec(task_id=task_id, title="x", body="y", issue_number=1)

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None:
        pass

    def file_followup(self, title: str, body: str, labels=None) -> str:
        self._calls += 1
        if self._calls == 1:
            raise RuntimeError("gh flaked")
        self.followups.append({"title": title, "body": body, "labels": labels})
        return f"https://example.test/issues/{self._calls}"

    def publish_note(self, task_id: str, body: str, *, pr_url=None) -> None:
        self.notes.append(body)


def test_failed_followup_does_not_dedup_a_matching_improvement(tmp_path, project) -> None:
    # #190: filed_fps must exclude ref=None (filing-failed) findings. A finding whose
    # file_followup raised must NOT suppress an identically-titled improvement — otherwise a
    # transient filing failure leaves NEITHER in the tracker. Here the finding's filing fails
    # and the improvement shares its title; the improvement must still file as an enhancement.
    project._task_source = _FailFirstSource()
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    shared = "Type abandon() disposition as a Literal"
    _drive(eng, review_output={
        "approved": True, "issues": [],
        "non_blocking": [{"title": shared, "detail": "d", "disposition": "file"}],
        "improvement": {"title": shared, "detail": "same idea, restated",
                        "disposition": "file"},
    })

    ts = project.task_source
    # the finding's filing failed (ref=None); the improvement is NOT deduped and DOES file
    assert [f["labels"] for f in ts.followups] == [["enhancement"]]
    events = _events(tmp_path)
    assert not any(e["type"] == "improvement_deduped" for e in events)
    assert sum(e["type"] == "followup_failed" for e in events) == 1
    completed = next(e for e in events if e["type"] == "task_completed")
    assert completed["improvement_filed"] is True


class _BareSource:
    """A v1-era task source: resolve + mark_complete only, no evidence-out hooks."""

    def __init__(self) -> None:
        self.completed: list = []

    def resolve(self, task_id: str) -> TaskSpec:
        return TaskSpec(task_id=task_id, title="x", body="y", issue_number=1)

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None:
        self.completed.append((task_id, pr_url))


def test_finalize_graceful_when_adapter_lacks_hooks(tmp_path, project) -> None:
    project._task_source = _BareSource()
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    outcomes = _drive(eng, review_output=_REVIEW_WITH_FINDINGS)

    # No hooks -> silent no-op, task still completes cleanly.
    assert outcomes[-1]["outcome"] == "task_completed"
    assert project.task_source.completed == [("t1", "https://github.com/x/y/pull/1234")]


class _FlakySource:
    """file_followup raises; publish_note works. Finalize must survive."""

    def __init__(self) -> None:
        self.notes: list = []

    def resolve(self, task_id: str) -> TaskSpec:
        return TaskSpec(task_id=task_id, title="x", body="y", issue_number=1)

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None:
        pass

    def file_followup(self, title: str, body: str, labels=None) -> str:
        raise RuntimeError("gh exploded")

    def publish_note(self, task_id: str, body: str, *, pr_url=None) -> None:
        self.notes.append(body)


def test_finalize_survives_a_flaky_followup_hook(tmp_path, project) -> None:
    project._task_source = _FlakySource()
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    outcomes = _drive(eng, review_output=_REVIEW_WITH_FINDINGS)

    assert outcomes[-1]["outcome"] == "task_completed"  # never crashes finalize
    events = _events(tmp_path)
    assert sum(e["type"] == "followup_failed" for e in events) == 2
    # the note still publishes, marking the failed filings
    assert project.task_source.notes and "(filing failed)" in project.task_source.notes[0]


# --- render_completion_note --------------------------------------------------------


def test_render_completion_note_content() -> None:
    task = Task(
        task_id="#10", run_id="r1", created_at="t0", updated_at="t0",
        title="Checkpoint-tag GC", pr_url="https://github.com/o/r/pull/23",
    )
    task.stages[Stage.IMPLEMENT].model = "claude-opus-5"
    task.stages[Stage.IMPLEMENT].status = StageStatus.COMPLETED
    task.stages[Stage.REVIEW].output = {
        "approved": True, "issues": ["a lingering blocker"],
        "non_blocking": [{"title": "polish", "detail": "d", "disposition": "file"}],
    }

    note = render_completion_note(
        task, [{"title": "polish", "ref": "url-1"}, {"title": "other", "ref": None}]
    )

    assert "Checkpoint-tag GC" in note
    assert "https://github.com/o/r/pull/23" in note
    assert "✅ approved" in note
    assert "`claude-opus-5`" in note  # stage table renders per-stage model
    assert "a lingering blocker" in note  # blocking issues surfaced
    assert "polish → url-1" in note
    assert "other → (filing failed)" in note


def test_completion_note_costs_read_na_on_the_interactive_lane() -> None:
    from orchestrator.schemas.enums import ExecutionMode

    task = Task(task_id="#11", run_id="r1", created_at="t0", updated_at="t0", title="X")
    rec = task.stages[Stage.IMPLEMENT]
    rec.status = StageStatus.COMPLETED
    rec.model = "claude-opus-5"
    rec.lane = ExecutionMode.INTERACTIVE
    rec.cost_usd = 0.0  # interactive can't meter in-session

    note = render_completion_note(task, [])

    assert "n/a" in note  # not a misleading "$0.0000"
    assert "$0.0000" not in note


def test_render_completion_note_denied_verdict() -> None:
    task = Task(task_id="#10", run_id="r1", created_at="t0", updated_at="t0")
    task.stages[Stage.REVIEW].output = {"approved": False, "issues": ["broken"]}
    note = render_completion_note(task, [])
    assert "❌ changes requested" in note


def test_render_note_self_improvement_sections() -> None:
    task = Task(task_id="#9", run_id="r1", created_at="t0", updated_at="t0")
    task.stages[Stage.REVIEW].output = {
        "approved": True, "issues": [],
        "improvement": {"title": "Cost-aware routing", "detail": "route by budget"},
        "retrospective": {"title": "Batch lane untested", "detail": "run a batch"},
    }
    note = render_completion_note(task, [], improvement_ref="https://x/issues/50")
    assert "💡 **Improvement idea:** Cost-aware routing" in note
    assert "https://x/issues/50" in note  # links the filed enhancement
    assert "🔍 **Process retrospective:** Batch lane untested" in note

    # retrospective still renders when there's no improvement
    task.stages[Stage.REVIEW].output = {
        "approved": True, "issues": [], "retrospective": {"title": "Lesson only", "detail": "d"}}
    note2 = render_completion_note(task, [])
    assert "### Self-improvement" in note2 and "Lesson only" in note2
    assert "Improvement idea" not in note2


def test_render_note_never_claims_raw_fixup_disposition_was_applied() -> None:
    """Legacy/hand-built completion evidence has no #227 rework proof."""
    task = Task(task_id="#9", run_id="r1", created_at="t0", updated_at="t0")
    task.stages[Stage.REVIEW].output = {
        "approved": True,
        "issues": [],
        "improvement": {"title": "Tighten the guard", "disposition": "fixup"},
    }

    note = render_completion_note(task, [])

    assert "requested in-place fixup — not applied" in note
    assert "applied in place" not in note


# --- adapter hooks -----------------------------------------------------------------


def test_github_source_publish_note_and_file_followup() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        return "https://github.com/o/r/issues/99\n"

    src = GitHubIssuesSource("o/r", runner=runner)

    src.publish_note("#10", "hello", pr_url="https://github.com/o/r/pull/23")
    assert calls[-1] == ["gh", "pr", "comment", "https://github.com/o/r/pull/23",
                         "--body", "hello"]

    src.publish_note("#10", "hi")  # no PR -> comment on the issue
    assert calls[-1] == ["gh", "issue", "comment", "10", "--repo", "o/r", "--body", "hi"]

    ref = src.file_followup("Title", "Body", labels=["deferred-scope", "dx"])
    assert calls[-1] == ["gh", "issue", "create", "--repo", "o/r", "--title", "Title",
                         "--body", "Body", "--label", "deferred-scope", "--label", "dx"]
    assert ref == "https://github.com/o/r/issues/99"


def test_github_source_create_task_returns_hash_ref(tmp_path) -> None:
    # The spec front door's filing hook (#18): thin `gh issue create`, ref parsed to #N.
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        return "https://github.com/o/r/issues/42\n"

    src = GitHubIssuesSource("o/r", runner=runner)
    ref = src.create_task("New task", "the body", labels=["spec:x", "frontend"])
    assert calls[-1] == ["gh", "issue", "create", "--repo", "o/r", "--title", "New task",
                         "--body", "the body", "--label", "spec:x", "--label", "frontend"]
    assert ref == "#42"  # translated from the URL for Depends-on lines


def test_localfile_source_publish_note_and_file_followup(tmp_path) -> None:
    src = LocalFileTaskSource(tmp_path / "tasks.json")

    src.publish_note("id-1", "the note body", pr_url=None)
    assert "the note body" in (tmp_path / "notes.log").read_text()

    ref = src.file_followup("A title", "A body", labels=["deferred-scope"])
    assert ref == "local:A title"
    log = (tmp_path / "followups.log").read_text()
    assert "deferred-scope" in log and "A title" in log


def test_localfile_source_create_task_appends_and_returns_id(tmp_path) -> None:
    import json

    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"t1": {"title": "existing", "body": "x"}}))
    src = LocalFileTaskSource(path)

    ref = src.create_task("New", "body", labels=["spec:x"])
    assert ref == "t2"
    data = json.loads(path.read_text())
    assert data["t2"] == {"title": "New", "body": "body", "labels": ["spec:x"]}


def test_localfile_source_preserves_created_dependency_metadata(tmp_path) -> None:
    path = tmp_path / "tasks.json"
    path.write_text("{}")
    src = LocalFileTaskSource(path)
    ref = src.create_task("Child", "work\n\nDepends-on: t1, t2", labels=["child"])

    assert src.resolve(ref).depends_on == ["t1", "t2"]
    assert [task.task_id for task in src.list_tasks(label="child")] == [ref]


# --- review schema -----------------------------------------------------------------


def test_review_schema_accepts_non_blocking() -> None:
    schema = resolve_stage_schema("review")
    assert "non_blocking" in schema["properties"]
    non_blocking_schema = schema["properties"]["non_blocking"]
    assert "Only an explicit `file`" in non_blocking_schema["description"]
    disposition_schema = non_blocking_schema["items"]["properties"]["disposition"]
    assert "absent disposition is noted, not filed" in disposition_schema["description"]
    improvement_schema = schema["properties"]["improvement"]
    assert "only when it carries an explicit `file`" in improvement_schema["description"]
    assert (
        "absent disposition is noted, not filed"
        in improvement_schema["properties"]["disposition"]["description"]
    )
    validator = jsonschema.Draft202012Validator(schema)

    validator.validate({
        "approved": True, "issues": [],
        "non_blocking": [{"title": "x", "detail": "y"}, {"title": "z"}],
    })
    # a finding missing the required title is a schema violation
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({
            "approved": True, "issues": [], "non_blocking": [{"detail": "no title"}],
        })

    # #188: the optional per-finding disposition enum is accepted...
    validator.validate({
        "approved": True, "issues": [],
        "non_blocking": [
            {"title": "a", "disposition": "file"},
            {"title": "b", "disposition": "fix_now"},
            {"title": "c", "disposition": "drop"},
        ],
    })
    # ...a missing disposition still validates (and degrades safely to note-only)...
    validator.validate({"approved": True, "issues": [], "non_blocking": [{"title": "d"}]})
    # ...and an out-of-enum disposition is rejected
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({
            "approved": True, "issues": [],
            "non_blocking": [{"title": "e", "disposition": "maybe"}],
        })

    # self-improvement loop fields are optional objects with a required title
    assert "improvement" in schema["properties"] and "retrospective" in schema["properties"]
    validator.validate({
        "approved": True, "issues": [],
        "improvement": {"title": "idea", "detail": "why"},
        "retrospective": {"title": "lesson", "detail": "what"},
    })
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"approved": True, "issues": [], "improvement": {"detail": "no title"}})

    # #223: the improvement's optional disposition enum (file|fixup|fix_now|drop) validates...
    for disp in ("file", "fixup", "fix_now", "drop"):
        validator.validate({
            "approved": True, "issues": [],
            "improvement": {"title": "idea", "disposition": disp},
        })
    # ...and an out-of-enum improvement disposition is rejected
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({
            "approved": True, "issues": [],
            "improvement": {"title": "idea", "disposition": "maybe"},
        })
