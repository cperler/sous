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
    render_html,
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
    # #524: the digest headline — per-state counts, wall time, whole-run spend — and a
    # per-task issue number and cost, so one mail answers "what did this batch do".
    assert roster["t1"]["issue_number"] == 42
    assert roster["t1"]["cost"]["invocations"] > 0
    assert final["counts"] == {"completed": 1, "total": 1}
    assert final["duration_s"] >= 0
    assert final["cost"]["usd"] == roster["t1"]["cost"]["usd"]
    assert "unmetered_calls" in final["cost"]


def test_completed_payload_carries_issue_pr_review_and_stage_detail(tmp_path) -> None:
    """#524: enough to judge the task without opening the tracker — the ask, what changed
    on the PR, the review outcome, and per-stage model/tokens/cost."""
    project, calls = _recording_project()
    project.task_source.issue_url = lambda tid: f"https://example.test/issues/{tid}"
    project.task_source.spec_overrides["t1"] = {
        "labels": ["enhancement", "ux"],
        "body": "## Problem\nThe mails repeat themselves.\n\n## Acceptance\n- no repeats",
    }
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _drive(eng)

    payload = next(p for k, p in calls if k == NOTIFY_TASK_COMPLETED)
    assert payload["issue_url"] == "https://example.test/issues/t1"
    assert payload["labels"] == ["enhancement", "ux"]
    assert payload["issue_excerpt"] == "The mails repeat themselves."
    assert payload["issue_acceptance"] == "- no repeats"

    # The PR summary comes from the SAME describe_pr read as #378's delivery evidence.
    pr = payload["pr"]
    assert pr["title"] == "Fake PR" and pr["checks"] == "success"
    assert (pr["additions"], pr["deletions"], pr["changed_files"]) == (12, 3, 1)
    assert pr["files"] == [{"path": "a.py", "additions": 12, "deletions": 3}]
    assert pr["commits"] == [{"sha": "abc123def456", "title": "Do it"}]

    assert payload["review"]["approved"] is True
    assert payload["review"]["cycles"] == 0
    implement = next(s for s in payload["stages"] if s["stage"] == Stage.IMPLEMENT.value)
    assert {"model", "effort", "lane", "input_tokens", "output_tokens", "cost_usd",
            "metered", "duration_s"} <= set(implement)
    assert payload["followups"] == []

    # The note mailed is the note published, and it now carries the ledger total — the
    # same figure as the payload's cost block, so the two cannot disagree.
    assert f"${payload['cost']['usd']:.4f}" in payload["note_md"]
    assert "**Total:**" in payload["note_md"]


def test_labels_are_snapshotted_and_refreshed_with_the_spec(tmp_path) -> None:
    project, _calls = _recording_project()
    project.task_source.spec_overrides["t1"] = {"labels": ["bug"]}
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    assert eng.store.load_task("r1", "t1").labels == ["bug"]

    project.task_source.spec_overrides["t1"] = {"labels": ["bug", "ux"], "body": "edited"}
    eng.refresh_spec("r1", "t1")
    assert eng.store.load_task("r1", "t1").labels == ["bug", "ux"]


def test_failed_describe_pr_thins_the_mail_and_is_evented(tmp_path) -> None:
    """Enrichment stays total: an unreadable PR drops the PR block, never the completion,
    and the trail says why the mail is thinner."""
    project, calls = _recording_project()

    def _boom(url: str) -> dict:
        raise RuntimeError("gh unreachable")

    project.task_source.describe_pr = _boom
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    outcomes = _drive(eng)

    assert outcomes[-1]["outcome"] == "task_completed"
    payload = next(p for k, p in calls if k == NOTIFY_TASK_COMPLETED)
    assert "pr" not in payload
    assert payload["pr_url"] == "https://github.com/x/y/pull/1234"  # the link survives
    degraded = [e for e in _events(tmp_path) if e["type"] == "notification_facts_degraded"]
    assert [e["part"] for e in degraded] == ["pr"]


def test_rejected_pr_carries_its_delivery_verdict_to_both_mails(tmp_path) -> None:
    """A PR the #378 check rejects (here: CLOSED) is still described for the mail, but the
    verdict rides along so neither the task mail nor the run digest says "merge it"."""
    project, calls = _recording_project()
    project.task_source.pr_info["state"] = "CLOSED"
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _drive(eng)

    pr = next(p for k, p in calls if k == NOTIFY_TASK_COMPLETED)["pr"]
    assert pr["state"] == "CLOSED"
    assert "CLOSED" in pr["delivery_problem"]
    entry = next(p for k, p in calls if k == "run_finalized")["tasks"][0]
    assert (entry["pr_state"], entry["delivery_verified"]) == ("CLOSED", False)


def test_validated_pr_has_no_delivery_problem(tmp_path) -> None:
    project, calls = _recording_project()
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _drive(eng)

    assert next(p for k, p in calls if k == NOTIFY_TASK_COMPLETED)["pr"][
        "delivery_problem"] is None
    entry = next(p for k, p in calls if k == "run_finalized")["tasks"][0]
    assert (entry["pr_state"], entry["delivery_verified"]) == ("OPEN", True)


def test_failure_and_park_paths_never_read_the_pr(tmp_path) -> None:
    """The failure/park alerts stay offline (#409): the facts builder never calls the
    tracker itself, only the completion path hands it a PR it already read."""
    project, calls = _recording_project()
    reads: list[str] = []
    project.task_source.describe_pr = lambda url: reads.append(url) or {}
    eng = _engine(tmp_path, project, max_attempts=1)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))
    eng.record("r1", make_result(eng.next_work("r1", "t1"), status=ResultStatus.FAILURE,
                                 error="boom", structured_output={}))

    failed = next(p for k, p in calls if k == "task_failed")
    assert reads == [] and "pr" not in failed
    assert "labels" in failed and "issue_url" in failed and "review" in failed


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


_NOTE = """## Orchestration run complete — #524

- **Task:** Add an email sink
- **PR:** https://github.com/x/y/pull/1234
- **Review:** ✅ approved

| # | Stage | Status | Model | Effort | In | Out | Cost |
|---:|---|---|---|---|---:|---:|---:|
| 01 | implement | completed | `claude-opus` | high | 1.2k | 300 | $1.2345 |

**Total:** $1.2345 over 7 model call(s)
"""


def _completed_payload(**extra) -> dict:
    return {
        "run_id": "r1", "task_id": "#524", "kind": NOTIFY_TASK_COMPLETED,
        "summary": "task #524 COMPLETED — Add an email sink (https://github.com/x/y/pull/1234)",
        "title": "Add an email sink", "issue_number": 524,
        "issue_url": "https://github.com/x/y/issues/524",
        "pr_url": "https://github.com/x/y/pull/1234", "pr_number": 1234,
        "cost": {"usd": 1.2345, "invocations": 7, "unmetered_calls": 0},
        "stages": [{"stage": "implement", "status": "completed", "attempt": 1,
                    "model": "claude-opus", "lane": "headless", "cost_usd": 1.2345,
                    "metered": True, "error": None}],
        "pr": {"url": "https://github.com/x/y/pull/1234", "number": 1234,
               "title": "Add an email sink", "state": "OPEN", "checks": "success",
               "additions": 10, "deletions": 2, "changed_files": 1,
               "files": [{"path": "sink.py", "additions": 10, "deletions": 2}],
               "commits": [{"sha": "abc123", "title": "Add the sink"}]},
        "run_dir": "/runs/r1",
        "note_md": _NOTE,
        **extra,
    }


def test_message_carries_the_actionable_facts() -> None:
    cfg = config_from_env(_ENV)
    assert cfg is not None
    msg = build_message(cfg, NOTIFY_TASK_COMPLETED, _completed_payload())

    assert msg["To"] == "craig@example.test, ops@example.test"
    assert msg["From"] == "bot@example.test"
    assert msg["X-Orchestrator-Run"] == "r1"
    assert msg["X-Orchestrator-Kind"] == NOTIFY_TASK_COMPLETED
    subject = msg["Subject"]
    assert subject == "[orchestrator] COMPLETED #524 — Add an email sink (PR #1234)"

    text = msg.get_body(preferencelist=("plain",)).get_content()
    assert text.startswith("Next: review and merge https://github.com/x/y/pull/1234")
    assert "+10 −2 across 1 file(s), 1 commit(s)" in text  # the diffstat
    assert "$1.2345 over 7 model call(s)" in text  # from the embedded note
    assert "/runs/r1" in text  # pointer to the full trail

    # #524: an HTML alternative with the same content, alongside the complete text part.
    assert msg.is_multipart()
    rich = msg.get_body(preferencelist=("html",)).get_content()
    assert '<a href="https://github.com/x/y/pull/1234">' in rich
    assert "<table" in rich and "sink.py" in rich


def test_completed_mail_shows_each_fact_once() -> None:
    """#524's core complaint: the subject, first line, note header and stage list said the
    same things. Each fact now appears exactly once across subject and body."""
    payload = _completed_payload()
    whole = render_subject(NOTIFY_TASK_COMPLETED, payload) + "\n" + render_body(
        NOTIFY_TASK_COMPLETED, payload
    )
    assert whole.count("https://github.com/x/y/pull/1234") == 1
    assert whole.count("Add an email sink") == 1  # PR title == task title is not repeated
    assert whole.count("#524") == 1
    assert whole.count("PR #1234") == 1
    assert whole.count("over 7 model call(s)") == 1  # the note's total; no second one
    assert whole.count("| Stage |") == 1  # the note's stage table; no second one
    assert "COMPLETED" not in render_body(NOTIFY_TASK_COMPLETED, payload)  # summary dropped


def test_mail_without_the_note_renders_review_stages_and_cost_itself() -> None:
    """A failure, a park, or a degraded completion has no note: the sink renders the same
    facts from the payload's structured blocks instead."""
    payload = _completed_payload(note_md=None, review={
        "approved": False, "cycles": 2, "blocking": ["high — a.py:3 — breaks X"],
        "non_blocking": [{"title": "rename y", "disposition": "file"}],
    }, followups=[{"title": "rename y", "ref": "https://github.com/x/y/issues/9"}])
    body = render_body(NOTIFY_TASK_COMPLETED, payload)
    assert "Verdict: changes requested" in body
    assert "- high — a.py:3 — breaks X" in body
    assert "- rename y (file)" in body
    assert "implement  completed  claude-opus" in body  # the stage table
    assert "Total: $1.2345 over 7 model call(s)" in body
    assert "- rename y: https://github.com/x/y/issues/9" in body


@pytest.mark.parametrize("pr_extra", [
    {"state": "CLOSED"},
    {"state": "OPEN", "delivery_problem": "PR head x does not match task/524"},
    {"state": "MERGED", "delivery_problem": "PR head SHA a does not match delivered b"},
])
def test_completed_mail_never_says_merge_for_a_rejected_pr(pr_extra) -> None:
    """The #378 check judged the PR invalid: the reader must be told to look, not merge
    (and a merged-but-mismatched PR is not "nothing to do" either)."""
    payload = _completed_payload()
    payload["pr"] = {**payload["pr"], **pr_extra}
    body = render_body(NOTIFY_TASK_COMPLETED, payload)
    first = body.splitlines()[0]
    assert first.startswith("Next: do not merge yet.")
    assert "delivery could not be verified" in first
    assert "review and merge" not in body and "already merged" not in body
    if problem := pr_extra.get("delivery_problem"):
        assert f"Delivery problem: {problem}" in body


def test_run_digest_counts_only_verified_unmerged_prs_as_ready() -> None:
    def task(tid: str, **extra) -> dict:
        return {"task_id": tid, "state": "completed",
                "pr_url": f"https://github.com/x/y/pull/{tid}", **extra}

    body = render_body("run_finalized", {"run_id": "r1", "state": "completed", "tasks": [
        task("1", pr_state="OPEN", delivery_verified=True),
        task("2", pr_state="MERGED", delivery_verified=True),
        task("3", pr_state="CLOSED", delivery_verified=False),
        task("4"),  # an older payload with no verdict still counts as ready
        {"task_id": "5", "state": "failed", "pr_url": None},
    ]})
    assert "Next: review and merge the 2 PR(s) below." in body
    assert "Check first: 1 PR(s) failed the delivery check" in body
    assert "2 completed (PR merged)" in body
    assert "3 completed (PR delivery unverified)" in body


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
    assert subject == (
        "[orchestrator] ACTION NEEDED: #390 parked before deliver — Meta-authoring change"
    )

    body = render_body(NOTIFY_TASK_BLOCKED, payload)
    assert "Gate: before:deliver" in body
    assert "Held before: deliver" in body
    assert "https://github.com/cperler/sous/issues/390" in body
    assert "orchestrator approve --task #390" in body
    assert "orchestrator reject --task #390" in body
    # The command block comes BEFORE the context a recipient reads only if they care.
    assert body.index("orchestrator approve") < body.index("Total:")


def test_issue_number_and_issue_link_are_shown_once() -> None:
    """A real park payload carries BOTH issue_number (from the shared facts merge) and
    issue_url. The link already names the issue, so the number is not a second line."""
    body = render_body(NOTIFY_TASK_BLOCKED, {
        "task_id": "#390", "issue_number": 390,
        "issue_url": "https://github.com/cperler/sous/issues/390"})
    labels = [line.split(":", 1)[0] for line in body.splitlines() if ":" in line]
    assert len(labels) == len(set(labels)), f"duplicate fact labels in body:\n{body}"
    assert "Link: https://github.com/cperler/sous/issues/390" in body
    assert "390" not in body.replace("https://github.com/cperler/sous/issues/390", "")
    # The HTML part keeps the number as the anchor text of that one link.
    assert ">#390</a>" in render_html(NOTIFY_TASK_BLOCKED, {
        "issue_number": 390, "issue_url": "https://github.com/cperler/sous/issues/390"})


def test_other_kinds_keep_their_plain_subject_and_no_action_block() -> None:
    """Kinds outside the per-task/run layouts keep the plain ``kind — id`` subject, and a
    malformed ``actions`` value never produces an action block."""
    assert render_subject("task_stale", {"task_id": "t1"}).startswith(
        "[orchestrator] task_stale — t1"
    )
    assert render_subject("task_completed", {"task_id": "t1"}) == "[orchestrator] COMPLETED t1"
    body = render_body("task_completed", {"summary": "task t1 COMPLETED", "actions": "oops"})
    assert "ACTION NEEDED" not in body
    body = render_body("task_blocked", {"task_id": "t1", "actions": "oops"})
    assert "ACTION NEEDED" not in body


def test_html_escapes_payload_text() -> None:
    """Payload text is model- and tracker-authored: it must never inject markup."""
    rich = render_html(NOTIFY_TASK_COMPLETED, _completed_payload(
        issue_excerpt="<script>alert(1)</script>",
        note_md="- **Review:** <b>x</b> & `<i>`",
    ))
    assert "<script>" not in rich and "&lt;script&gt;" in rich
    assert "<b>x</b>" not in rich and "<code>&lt;i&gt;</code>" in rich


def test_html_part_is_bounded_without_cutting_a_tag() -> None:
    rich = render_html("task_completed", {"note_md": "- x\n" * 100_000})
    assert rich.startswith("<html>") and rich.rstrip().endswith("</html>")
    assert "<pre>" in rich and "… [truncated]" in rich


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
