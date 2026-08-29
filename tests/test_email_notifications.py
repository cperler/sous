"""Email alerting on task completion/failure (#359).

Two halves, tested independently because they are deliberately decoupled:

* **Engine side** — the new ``task_completed`` kind (the success half, previously missing
  entirely: a landed task was only observable at whole-run granularity via
  ``run_finalized``, which on a batch says nothing about WHICH task shipped), plus the
  shared payload enrichment that makes both per-task kinds actionable.
* **Adapter side** — a stdlib-SMTP sink that is a no-op unless the environment configures
  it, filters by kind, and swallows every failure. No SMTP under ``orchestrator/``.
"""

from __future__ import annotations

import json
import smtplib

import pytest

from adapters.project.email_sink import (
    EmailConfig,
    EmailSink,
    build_message,
    config_from_env,
    email_sink_from_env,
    render_body,
    render_subject,
)
from adapters.project.selfhost.config import SelfHostConfig
from orchestrator.alerting import NOTIFY_TASK_BLOCKED, NOTIFY_TASK_COMPLETED
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionLane, ResultStatus, Stage, TaskState
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result
from tests.test_decomposition import _decompose


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _recording_project():
    project = FakeProject()
    calls: list[tuple[str, dict]] = []
    project.notify = lambda kind, payload: calls.append((kind, payload))
    return project, calls


def _drive(eng: Engine, run="r1", task="t1") -> list:
    outcomes = []
    while (work := eng.next_work(run, task)) is not None:
        outcomes.append(eng.record(run, make_result(work)))
    return outcomes


def _events(tmp_path) -> list[dict]:
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- engine: the task_completed kind + payload enrichment ---------------------------


def test_completed_task_emits_one_enriched_notification(tmp_path) -> None:
    """The success half of the per-task pair, carrying enough to judge the fix without
    re-opening `status`: the PR link, the title, which stages ran, and the metered cost."""
    project, calls = _recording_project()
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    outcomes = _drive(eng)
    assert outcomes[-1]["outcome"] == "task_completed"

    completed = [p for k, p in calls if k == NOTIFY_TASK_COMPLETED]
    assert len(completed) == 1, "must fire exactly once per completed task"
    payload = completed[0]

    # The link the request asked for, both forms.
    assert payload["pr_url"] == "https://github.com/x/y/pull/1234"
    assert payload["pr_number"] == 1234
    assert payload["task_id"] == "t1"
    assert payload["run_id"] == "r1"
    assert payload["task_state"] == TaskState.COMPLETED.value
    assert payload["review_approved"] is True
    assert "t1" in payload["summary"] and "COMPLETED" in payload["summary"]

    # Stage outcomes: every stage that RAN, none that did not.
    ran = {s["stage"] for s in payload["stages"]}
    assert {Stage.INTAKE.value, Stage.IMPLEMENT.value, Stage.REVIEW.value} <= ran
    assert all(s["status"] != "pending" for s in payload["stages"])

    # Metered cost for THIS task, with the #319 unmetered count travelling alongside it.
    assert payload["cost"]["usd"] > 0
    assert payload["cost"]["invocations"] > 0
    assert "unmetered_calls" in payload["cost"]

    # Pointer to the retained run log dir, and the reused completion-note prose.
    assert payload["run_dir"] == str(tmp_path)
    assert "Orchestration run complete" in payload["note_md"]

    # And it is an audit row too, not only a hook call.
    rows = [e for e in _events(tmp_path)
            if e["type"] == "notification" and e.get("kind") == NOTIFY_TASK_COMPLETED]
    assert len(rows) == 1
    assert rows[0]["pr_url"] == "https://github.com/x/y/pull/1234"


def test_completion_note_rendered_once_for_both_consumers(tmp_path) -> None:
    """The alert's prose is the SAME artifact published to the PR — the engine never calls
    a model, so it reuses the note rather than authoring new prose."""
    project, calls = _recording_project()
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _drive(eng)

    payload = next(p for k, p in calls if k == NOTIFY_TASK_COMPLETED)
    assert project.task_source.notes[0]["body"] == payload["note_md"]


def test_umbrella_parent_completion_also_notifies(tmp_path, project) -> None:
    """The decomposition-parent path completes a task WITHOUT a stage result, so an emit
    hung off record()'s success branch alone would miss it. Both paths funnel through
    ``_on_task_completed``, which is why the emit lives there."""
    calls: list[tuple[str, dict]] = []
    project.notify = lambda kind, payload: calls.append((kind, payload))
    eng, _source = _decompose(tmp_path, project)

    parent = eng.store.load_task("r1", "parent")
    for child in parent.decomposition_children:
        _drive(eng, task=child)

    assert eng.store.load_task("r1", "parent").state is TaskState.COMPLETED
    notified = [p["task_id"] for k, p in calls if k == NOTIFY_TASK_COMPLETED]
    assert "parent" in notified
    assert notified.count("parent") == 1


def test_failed_task_payload_stays_backward_compatible(tmp_path) -> None:
    """Enrichment is ADDITIVE: the three original keys keep their exact meaning so every
    existing consumer is untouched."""
    project, calls = _recording_project()
    eng = _engine(tmp_path, project, max_attempts=1)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    work = eng.next_work("r1", "t1")
    eng.record("r1", make_result(work, status=ResultStatus.FAILURE, error="boom",
                                 structured_output={}))

    failed = next(p for k, p in calls if k == "task_failed")
    assert failed["reason"] == "boom"
    assert failed["stage"] == work.stage.value
    assert "FAILED" in failed["summary"] and "boom" in failed["summary"]
    # ...and now also carries the shared facts.
    assert failed["task_state"] == TaskState.FAILED.value
    assert failed["run_dir"] == str(tmp_path)
    assert failed["cost"]["invocations"] > 0
    assert any(s["stage"] == work.stage.value for s in failed["stages"])


def test_run_finalized_carries_per_task_roster(tmp_path) -> None:
    """A batch digest needs to name which task landed where — a completed/total count
    cannot."""
    project, calls = _recording_project()
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _drive(eng)

    final = next(p for k, p in calls if k == "run_finalized")
    roster = {t["task_id"]: t for t in final["tasks"]}
    assert roster["t1"]["state"] == TaskState.COMPLETED.value
    assert roster["t1"]["pr_url"] == "https://github.com/x/y/pull/1234"


def test_notify_hook_failure_never_breaks_the_completion(tmp_path) -> None:
    """A dead SMTP server (or any raising sink) must not un-complete a finished task."""
    project = FakeProject()

    def _boom(kind: str, payload: dict) -> None:
        raise RuntimeError("smtp unreachable")

    project.notify = _boom
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    outcomes = _drive(eng)
    assert outcomes[-1]["outcome"] == "task_completed"
    assert eng.store.load_task("r1", "t1").state is TaskState.COMPLETED
    assert any(e["type"] == "notify_failed" for e in _events(tmp_path))


# --- adapter: the email sink -------------------------------------------------------


_ENV = {
    "ORCHESTRATOR_SMTP_HOST": "smtp.example.test",
    "ORCHESTRATOR_NOTIFY_EMAIL_TO": "craig@example.test, ops@example.test",
    "ORCHESTRATOR_SMTP_USER": "bot@example.test",
    "ORCHESTRATOR_SMTP_PASSWORD": "app-password",
}


@pytest.mark.parametrize("env", [
    {},  # nothing configured at all
    {"ORCHESTRATOR_SMTP_HOST": "smtp.example.test"},  # host but no recipient
    {"ORCHESTRATOR_NOTIFY_EMAIL_TO": "craig@example.test"},  # recipient but no host
])
def test_sink_absent_unless_configured(env) -> None:
    """The quiet default: an unconfigured machine behaves exactly as it did before #359."""
    assert config_from_env(env) is None
    assert email_sink_from_env(env) is None


def test_suite_runs_with_no_smtp_config_in_environ() -> None:
    """Regression: the suite must never see the operator's REAL alerting config.

    The selfhost adapter's notify hook resolves `email_sink_from_env()` against live
    `os.environ`, so without the session-scoped scrub in conftest a full-suite run on a
    machine with SMTP configured mails the operator fixture events ("T1 — Tidy a module,
    PR #1234"). Asserting the no-arg (os.environ) resolution yields no sink pins the
    scrub in place; on an unconfigured machine this is vacuously green.
    """
    assert config_from_env() is None
    assert email_sink_from_env() is None


def test_config_defaults_and_overrides() -> None:
    cfg = config_from_env(_ENV)
    assert cfg is not None
    assert cfg.recipients == ("craig@example.test", "ops@example.test")
    assert cfg.port == 587 and cfg.starttls is True and cfg.use_ssl is False
    assert cfg.sender == "bot@example.test"  # falls back to the user
    assert cfg.timeout_s == 10.0  # a hanging connection cannot stall the scheduler
    assert cfg.kinds is None  # default: mail every kind

    ssl_cfg = config_from_env({**_ENV, "ORCHESTRATOR_SMTP_SSL": "1"})
    assert ssl_cfg is not None
    assert ssl_cfg.use_ssl is True and ssl_cfg.port == 465
    assert ssl_cfg.starttls is False, "STARTTLS is meaningless on an implicit-TLS connection"


def test_malformed_numeric_overrides_fall_back_instead_of_raising() -> None:
    """This is parsed inside an alerting path — a typo'd port must not raise there."""
    cfg = config_from_env({**_ENV, "ORCHESTRATOR_SMTP_PORT": "not-a-port",
                           "ORCHESTRATOR_SMTP_TIMEOUT_S": ""})
    assert cfg is not None
    assert cfg.port == 587 and cfg.timeout_s == 10.0


def test_kind_allowlist_filters() -> None:
    sent: list = []
    env = {**_ENV, "ORCHESTRATOR_NOTIFY_EMAIL_KINDS": "task_completed,task_failed"}
    sink = email_sink_from_env(env, transport=lambda cfg, msg: sent.append(msg))
    assert sink is not None

    assert sink("task_completed", {"summary": "landed"}) is True
    assert sink("task_stale", {"summary": "noisy"}) is False
    assert [m["X-Orchestrator-Kind"] for m in sent] == ["task_completed"]


def test_message_carries_the_actionable_facts() -> None:
    cfg = config_from_env(_ENV)
    assert cfg is not None
    payload = {
        "run_id": "r1", "task_id": "t1", "kind": NOTIFY_TASK_COMPLETED,
        "summary": "task t1 COMPLETED", "title": "Add an email sink",
        "pr_url": "https://github.com/x/y/pull/1234", "pr_number": 1234,
        "cost": {"usd": 1.2345, "invocations": 7, "unmetered_calls": 0},
        "stages": [{"stage": "implement", "status": "completed", "attempt": 1,
                    "model": "claude-opus", "error": None}],
        "run_dir": "/runs/r1",
        "note_md": "## what changed\n- added the sink",
    }
    msg = build_message(cfg, NOTIFY_TASK_COMPLETED, payload)

    assert msg["To"] == "craig@example.test, ops@example.test"
    assert msg["From"] == "bot@example.test"
    assert msg["X-Orchestrator-Run"] == "r1"
    subject = msg["Subject"]
    assert "task_completed" in subject and "t1" in subject and "PR #1234" in subject

    body = msg.get_content()
    assert "https://github.com/x/y/pull/1234" in body  # the link asked for
    assert "$1.2345" in body and "7 model call(s)" in body
    assert "implement: completed" in body
    assert "/runs/r1" in body  # pointer to the full trail
    assert "added the sink" in body  # the "what was done" prose


def test_park_mail_leads_with_the_release_commands() -> None:
    """#409: a park alert is a REQUEST, not a digest line — the subject says so and the
    body carries the gate, the issue link, and the commands, above the cost/stage detail."""
    payload = {
        "run_id": "batch-390-406", "task_id": "#390", "kind": NOTIFY_TASK_BLOCKED,
        "summary": "task #390 BLOCKED_ON_HUMAN at deliver (before:deliver)",
        "title": "Meta-authoring change", "stage": "deliver", "hold_before": "deliver",
        "gate": "before:deliver", "reason": "held at the before:deliver checkpoint",
        "issue_url": "https://github.com/cperler/sous/issues/390",
        "cost": {"usd": 0.5, "invocations": 2, "unmetered_calls": 0},
        "actions": [
            {"label": "approve — release the gate", "command": "orchestrator approve --task #390"},
            {"label": "reject — close it", "command": "orchestrator reject --task #390"},
        ],
    }
    subject = render_subject(NOTIFY_TASK_BLOCKED, payload)
    assert subject.startswith("[orchestrator] ACTION NEEDED task_blocked")

    body = render_body(NOTIFY_TASK_BLOCKED, payload)
    assert "Gate: before:deliver" in body
    assert "Held before: deliver" in body
    assert "https://github.com/cperler/sous/issues/390" in body
    assert "orchestrator approve --task #390" in body
    assert "orchestrator reject --task #390" in body
    # The command block comes BEFORE the context a recipient reads only if they care.
    assert body.index("orchestrator approve") < body.index("Cost:")


def test_other_kinds_keep_their_plain_subject_and_no_action_block() -> None:
    """The new fields are additive: a kind without them renders exactly as before."""
    subject = render_subject("task_completed", {"task_id": "t1"})
    assert subject.startswith("[orchestrator] task_completed")
    body = render_body("task_completed", {"summary": "task t1 COMPLETED", "actions": "oops"})
    assert "ACTION NEEDED" not in body


def test_unmetered_cost_is_labelled_a_floor() -> None:
    """#319: never render a confident $0 for usage that was never recoverable."""
    body = render_body("task_completed", {
        "summary": "s", "cost": {"usd": 0.0, "invocations": 3, "unmetered_calls": 3}})
    assert "AT LEAST" in body


def test_render_tolerates_a_thin_or_degraded_payload() -> None:
    """The enrichment blocks are best-effort, and the poll-driven kinds are thin — a sink
    must never assume a key exists."""
    body = render_body("task_stale", {"summary": "task t1 STALLED"})
    assert body.startswith("task t1 STALLED")
    assert render_subject("run_paused", {"run_id": "r1"}).startswith("[orchestrator] run_paused")
    # A payload whose derived blocks are the wrong shape entirely still renders.
    assert render_body("task_failed", {"summary": "s", "cost": None, "stages": "oops"})


def test_body_is_bounded() -> None:
    body = render_body("task_completed", {"summary": "s", "note_md": "x" * 200_000})
    assert len(body) < 70_000
    assert body.endswith("… [truncated]")


@pytest.mark.parametrize("boom", [
    smtplib.SMTPException("rejected"),
    TimeoutError("connection hung"),
    OSError("network unreachable"),
])
def test_transport_failures_are_swallowed(boom) -> None:
    """An alert sink must never break a run — the caller is inside a terminal transition
    that cannot be replayed."""
    def _raise(cfg, msg):
        raise boom

    sink = EmailSink(config_from_env(_ENV), transport=_raise)  # type: ignore[arg-type]
    assert sink("task_completed", {"summary": "s"}) is False


def test_sink_does_not_open_a_socket_when_kind_is_filtered() -> None:
    """Filtering happens BEFORE the transport, so a narrowed allowlist costs nothing."""
    def _fail(cfg, msg):  # pragma: no cover - must not be reached
        raise AssertionError("transport called for a filtered kind")

    cfg = EmailConfig(host="h", port=25, recipients=("a@b.test",), sender="s@b.test",
                      kinds=frozenset({"task_failed"}))
    assert EmailSink(cfg, transport=_fail)("task_completed", {"summary": "s"}) is False


# --- adapter wiring ----------------------------------------------------------------


def test_selfhost_adapter_has_a_notify_hook(tmp_path, capsys) -> None:
    """Before #359 this adapter had none, so every dogfood batch was silent regardless of
    what the seam supported."""
    cfg = SelfHostConfig(tasks_path=str(tmp_path / "tasks.json"))
    assert callable(cfg.notify)

    cfg.notify("task_completed", {"summary": "task t1 COMPLETED"})
    assert "task t1 COMPLETED" in capsys.readouterr().err


def test_selfhost_notify_survives_a_broken_sink(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_SMTP_HOST", "smtp.invalid.test")
    monkeypatch.setenv("ORCHESTRATOR_NOTIFY_EMAIL_TO", "craig@example.test")

    def _boom(*a, **kw):
        raise RuntimeError("resolution failed")

    monkeypatch.setattr("adapters.project.selfhost.config.email_sink_from_env", _boom)
    SelfHostConfig(tasks_path=str(tmp_path / "tasks.json")).notify("task_failed", {"summary": "x"})
