"""#314 — the dispatch record captures the inputs that determine a call's cost.

Two halves of one defect: the rendered prompt was never persisted anywhere, and
``stage_dispatched`` dropped the ``session_ref`` it had just computed. Both made a
finished run unauditable for token spend — continuity in particular read as a confident
0% across a run where every long stage was in fact resuming a session.

These tests pin the durable record, not the internals: everything asserted here is read
back from ``runs/<run>/`` (``events.jsonl`` + the stage dir) the way a post-hoc auditor
with no live process would read it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from adapters.project.base import TaskSpec
from orchestrator import status_store as status_store_mod
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionLane, Provider, ResultStatus, Stage
from orchestrator.stages import STAGE_SPECS
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _dispatches(eng: Engine, run_id: str = "r1") -> list[dict]:
    return [e for e in eng.store.read_events(run_id) if e["type"] == "stage_dispatched"]


def _dispatch_for(eng: Engine, work, run_id: str = "r1") -> dict:
    matches = [e for e in _dispatches(eng, run_id) if e["work_item_id"] == work.id]
    assert len(matches) == 1, f"expected exactly one dispatch event for {work.id}"
    return matches[0]


# --- half (b): session_ref on the dispatch event -----------------------------------------


def test_stage_dispatched_carries_a_task_held_session_ref(tmp_path, project) -> None:
    """The headless-lane blind spot: a dispatch that resumes a session reads cache, and the
    timeline must say so. Before #314 this field was absent on every dispatch."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake, engine lane
    scope = eng.next_work("r1", "t1")
    eng.record("r1", make_result(scope, session_ref="sess-abc"))  # claude mints a ref

    implement = eng.next_work("r1", "t1")
    assert implement.session_ref == "sess-abc"  # the value actually handed to the transport
    assert _dispatch_for(eng, implement)["session_ref"] == "sess-abc"


def test_stage_dispatched_session_ref_is_null_on_a_fresh_dispatch(tmp_path, project) -> None:
    """Null, not absent — an absent key is what made continuity unmeasurable, so the
    no-session case has to be positively recorded."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    first = eng.next_work("r1", "t1")

    event = _dispatch_for(eng, first)
    assert "session_ref" in event  # present...
    assert event["session_ref"] is None  # ...and explicitly empty


def test_provider_mismatched_session_ref_shows_null_not_the_held_ref(tmp_path, project) -> None:
    """#9 gate: a claude conversation id means nothing to ``codex exec resume``, so it is
    suppressed at dispatch. The event must report what was SENT (null), not what the task
    still holds — otherwise the timeline claims a cache read that never happened."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", provider_tag="codex")  # IMPLEMENT routes to codex
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    scope = eng.next_work("r1", "t1")
    assert scope.lane_policy.provider is Provider.CLAUDE
    eng.record("r1", make_result(scope, session_ref="claude-sess"))

    implement = eng.next_work("r1", "t1")
    assert implement.lane_policy.provider is Provider.CODEX
    assert implement.session_ref is None  # suppressed for the transport...

    event = _dispatch_for(eng, implement)
    assert "session_ref" in event
    assert event["session_ref"] is None  # ...and the event agrees with the transport
    # the suppression is a per-dispatch gate, NOT a loss: the task still holds the claude
    # ref for the next claude stage, so the event is reporting the send, not the state.
    assert eng.store.load_task("r1", "t1").session_ref == "claude-sess"


def test_continuity_rate_is_derivable_from_events_alone(tmp_path, project) -> None:
    """The acceptance criterion, computed the way an auditor would: no task docs, no live
    process — just events.jsonl."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        # every model stage reports the same session back, so each following stage resumes
        eng.record("r1", make_result(w, session_ref="sess-abc"))

    raw = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    dispatched = [e for e in raw if e["type"] == "stage_dispatched"]
    resumed = [e for e in dispatched if e["session_ref"]]
    assert len(dispatched) > 2
    # A provider session only rides a stage dispatched to a PROVIDER lane. A deterministic
    # ENGINE-lane stage — INTAKE, and PUBLISH since #389 — has no provider process to
    # resume, so its dispatch is honestly session-less rather than falsely chained, and it
    # is the whole population of "fresh" here: intake's own result mints the ref before the
    # first model stage, so every provider dispatch in this run is warm.
    deterministic = [e for e in dispatched if STAGE_SPECS[Stage(e["stage"])].deterministic]
    provider_dispatches = [e for e in dispatched if e not in deterministic]
    assert len(resumed) == len(provider_dispatches)
    assert all(e["session_ref"] is None for e in deterministic)
    assert all("session_ref" in e for e in dispatched)  # no key-absent holes to guess at

    audit = eng.events_audit("r1")["continuity"]
    assert audit["known"] == len(dispatched) and audit["unknown"] == 0
    assert audit["resumed"] == len(resumed) and audit["fresh"] == len(deterministic)
    assert audit["rate"] == len(resumed) / len(dispatched)


def test_continuity_audit_reports_unknown_rather_than_a_false_zero(tmp_path, project) -> None:
    """Pre-#314 history carries no ``session_ref`` key. Reading that absence as "no session"
    is the exact misreading this issue was filed over, so those dispatches are counted as
    UNKNOWN and excluded from the rate — a `rate` of None, never a confident 0.0."""
    eng = _engine(tmp_path, project)
    old = [
        {"type": "stage_dispatched", "run_id": "r1", "task_id": "t1",
         "stage": "implement", "attempt": 0, "work_item_id": "wi-old"},
        {"type": "stage_recorded", "run_id": "r1", "task_id": "t1",
         "stage": "implement", "attempt": 0, "work_item_id": "wi-old"},
    ]
    audit = eng.events_audit("r1", events=old)["continuity"]

    assert audit["unknown"] == 1 and audit["known"] == 0
    assert audit["resumed"] == 0
    assert audit["rate"] is None  # "no data", distinguishable from "never resumed"


# --- half (a): the dispatched prompt is persisted -----------------------------------------


def test_dispatched_prompt_is_persisted_and_fingerprinted(tmp_path, project) -> None:
    """From the run dir alone: what prompt this stage was sent, plus a stable hash + length
    to diff prefixes across stages with."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    scope = eng.next_work("r1", "t1")

    event = _dispatch_for(eng, scope)
    persisted = tmp_path / event["prompt_file"]
    assert persisted.is_file()
    # byte-exact: the auditable artifact is the prompt itself, not a summary of it
    assert persisted.read_text(encoding="utf-8") == scope.prompt
    assert "do it" in persisted.read_text(encoding="utf-8")  # the issue body really is in there
    assert event["prompt_chars"] == len(scope.prompt)
    assert event["prompt_sha256"] == hashlib.sha256(scope.prompt.encode("utf-8")).hexdigest()
    # the advertised path is relative to the run root, so the run dir stays movable
    assert not Path(event["prompt_file"]).is_absolute()


def test_prompt_file_shares_the_stem_of_that_calls_stream(tmp_path, project) -> None:
    """The prompt lands next to the raw provider stream for the same call, under one stem —
    so "what did this call cost, and what was it sent?" is one directory listing."""
    from orchestrator.stream_probe import stream_relpath

    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    work = eng.next_work("r1", "t1")

    event = _dispatch_for(eng, work)
    stream = stream_relpath("t1", work.stage.value, work.attempt)
    assert event["prompt_file"] == stream.replace(".stream.jsonl", ".prompt.txt")


def test_each_attempt_keeps_its_own_prompt(tmp_path, project) -> None:
    """A retry re-renders the prompt (the failure learning is folded in), so clobbering the
    first attempt's copy would destroy the evidence of what actually changed between them."""
    eng = _engine(tmp_path, project, max_attempts=5, breaker_threshold=9)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # scope
    a0 = eng.next_work("r1", "t1")
    eng.record(
        "r1",
        make_result(a0, status=ResultStatus.FAILURE, error="boom", structured_output={}),
    )
    a1 = eng.next_work("r1", "t1")
    assert a1.stage is Stage.IMPLEMENT and a1.attempt == 1

    e0, e1 = _dispatch_for(eng, a0), _dispatch_for(eng, a1)
    assert e0["prompt_file"] != e1["prompt_file"]  # attempt-stemmed, so no clobber
    assert (tmp_path / e0["prompt_file"]).read_text(encoding="utf-8") == a0.prompt
    assert (tmp_path / e1["prompt_file"]).read_text(encoding="utf-8") == a1.prompt
    # and the fingerprints show the retry prompt genuinely differs from the first
    assert e0["prompt_sha256"] != e1["prompt_sha256"]


def test_resumed_dispatch_prompts_do_not_lose_the_superseded_lease(tmp_path, project) -> None:
    """Both dispatches of a resumed (#142) stage are recorded with a prompt fingerprint, so
    the burned call is auditable too — the waste #313 makes visible."""
    eng = _engine(tmp_path, project, max_attempts=5, breaker_threshold=9)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # scope
    w1 = eng.next_work("r1", "t1")  # dispatched, then "crashes"
    w2 = eng.next_work("r1", "t1", resume=True)

    for w in (w1, w2):
        event = _dispatch_for(eng, w)
        assert event["prompt_sha256"] == hashlib.sha256(w.prompt.encode("utf-8")).hexdigest()
        assert (tmp_path / event["prompt_file"]).is_file()


def test_oversized_prompt_truncates_loudly(tmp_path, project, monkeypatch) -> None:
    """"Never silent": the store RETURNS what it dropped and the engine call site emits the
    warning — and the hash stays over the FULL prompt, so prefix-drift comparison survives a
    truncation instead of being quietly poisoned by it."""
    # A pathologically large issue body — the shape the cap exists to bound — against a cap
    # lowered so the test stays fast. The prompt is genuinely oversized, not synthetically so.
    huge = "BODY " * 2000
    monkeypatch.setattr(
        project.task_source,
        "resolve",
        lambda task_id: TaskSpec(task_id=task_id, title="huge", body=huge, issue_number=42),
    )
    monkeypatch.setattr(status_store_mod, "STAGE_PROMPT_MAX_CHARS", 500)
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    work = eng.next_work("r1", "t1")
    assert len(work.prompt) > 500  # the cap really bites on a real rendered prompt

    events = eng.store.read_events("r1")
    notices = [e for e in events if e["type"] == "stage_prompt_truncated"]
    assert len(notices) == 1
    notice = notices[0]
    assert notice["severity"] == "warning"
    assert notice["work_item_id"] == work.id
    assert notice["prompt_chars"] == len(work.prompt)  # the TRUE size is reported
    assert notice["written_chars"] == 500
    assert notice["dropped_chars"] == len(work.prompt) - 500

    event = _dispatch_for(eng, work)
    # the fingerprint is of the whole prompt, not the capped file
    assert event["prompt_sha256"] == hashlib.sha256(work.prompt.encode("utf-8")).hexdigest()
    assert event["prompt_chars"] == len(work.prompt)
    # and the file itself admits it is partial, for a human who only opens the .txt
    text = (tmp_path / event["prompt_file"]).read_text(encoding="utf-8")
    assert text.startswith(work.prompt[:500])
    assert "truncated by orchestrator" in text


def test_normal_prompt_write_reports_no_truncation(tmp_path, project) -> None:
    """The 99% path emits no notice at all — the warning stays meaningful."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))

    events = eng.store.read_events("r1")
    assert not [e for e in events if e["type"] == "stage_prompt_truncated"]
    assert all(len(e["prompt_file"]) > 0 for e in _dispatches(eng))
