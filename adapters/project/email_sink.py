"""Email sink for the alerting seam (#359) — shared by every project adapter.

The engine emits enriched notification payloads and stays project-agnostic; DELIVERY is an
adapter concern, so no SMTP lives under ``orchestrator/``. This module is the reusable
delivery half, a sibling of ``github_issues`` — a project adapter's ``notify`` hook calls
``email_sink_from_env()`` and, if configured, hands it the payload.

Design decisions, and why:

* **stdlib ``smtplib``, not a transactional API.** Zero new dependencies, no HTTP client, no
  account provisioning — and it works unchanged against either a local relay or a hosted
  submission host (e.g. an app-password account). A transactional API would buy deliverability
  that a personal alerting channel does not need.
* **Configured from env only.** Nothing is checked in; an unconfigured environment yields NO
  sink at all (``None``), so the default behavior of every adapter is unchanged and no test
  or CI run can accidentally mail anyone.
* **Never breaks a run.** ``EmailSink.__call__`` swallows every exception and returns a bool.
  The engine already guards the ``notify`` hook (a raise is evented ``notify_failed``), so
  this is the second layer; the load-bearing addition is the SHORT SOCKET TIMEOUT, because
  the failure the engine's guard cannot cover is a connection that HANGS rather than raises —
  that would stall the scheduler mid-transition.
* **Transport is injected.** ``EmailSink`` takes a ``transport`` callable, so tests exercise
  the config, kind-filtering and rendering without opening a socket.

Environment:

===================================== ==========================================================
``ORCHESTRATOR_SMTP_HOST``            SMTP server hostname. **Required** — absent ⇒ no sink.
``ORCHESTRATOR_NOTIFY_EMAIL_TO``      Comma-separated recipients. **Required** — absent ⇒ no sink.
``ORCHESTRATOR_SMTP_PORT``            Port. Default 465 with SSL, else 587.
``ORCHESTRATOR_SMTP_USER``            Username for AUTH. Omit for an unauthenticated relay.
``ORCHESTRATOR_SMTP_PASSWORD``        Password / app password for AUTH.
``ORCHESTRATOR_SMTP_FROM``            Envelope sender. Defaults to the user, else orchestrator@localhost.
``ORCHESTRATOR_SMTP_SSL``            ``1`` to connect with implicit TLS (SMTP_SSL).
``ORCHESTRATOR_SMTP_STARTTLS``        ``0`` to disable STARTTLS on a plain connection (default on).
``ORCHESTRATOR_SMTP_TIMEOUT_S``       Socket timeout in seconds. Default 10.
``ORCHESTRATOR_NOTIFY_EMAIL_KINDS``   Comma-separated ``kind`` allowlist. Default: all kinds.
===================================== ==========================================================

On kinds: the default is to mail EVERYTHING. The human-gate kinds (``task_blocked``,
``run_paused``, ``run_blocked``) are arguably more urgent than completion — they stall the
batch until someone acts — so opting them out is a deliberate choice, not the default. Set
the allowlist to narrow it (e.g. ``task_completed,task_failed``).

#409 asked whether a run reaching PAUSED or exiting ``blocked_on_orphaned_dispatches``
should share this transport: they already do, as ``run_paused`` and ``run_blocked``, so
that class of "a human must act" event needed no new kind — only ``task_blocked`` gained
the ACTION-NEEDED subject and the ``actions`` block, because it is the one whose release
is a single specific command against a specific task. The park alert also closes its own
thread: releasing a task produces the ordinary ``task_completed`` / ``task_failed`` /
``run_finalized`` mail for whatever it goes on to do.
"""

from __future__ import annotations

import os
import smtplib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage

from orchestrator.alerting import NOTIFY_TASK_BLOCKED as _KIND_TASK_BLOCKED

# Envelope/format constants.
_DEFAULT_TIMEOUT_S = 10.0
_DEFAULT_PORT_SSL = 465
_DEFAULT_PORT_PLAIN = 587
_DEFAULT_SENDER = "orchestrator@localhost"
# The engine already bounds the prose it folds into a payload; this is the sink's own
# backstop so a hand-built or future payload can't produce a multi-megabyte message.
_MAX_BODY_CHARS = 60_000

Transport = Callable[["EmailConfig", EmailMessage], None]


def _flag(raw: str | None, *, default: bool) -> bool:
    """Parse a boolean env var. Unset/blank ⇒ ``default``; otherwise the usual truthy words."""
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(raw: str | None) -> tuple[str, ...]:
    """Split a comma-separated env var, dropping blanks."""
    return tuple(part.strip() for part in (raw or "").split(",") if part.strip())


@dataclass(frozen=True)
class EmailConfig:
    """Resolved SMTP settings. Frozen so a sink cannot be reconfigured mid-flight."""

    host: str
    port: int
    recipients: tuple[str, ...]
    sender: str
    username: str | None = None
    password: str | None = None
    use_ssl: bool = False
    starttls: bool = True
    timeout_s: float = _DEFAULT_TIMEOUT_S
    # None = mail every kind (the default). A frozenset = mail only these.
    kinds: frozenset[str] | None = None

    def wants(self, kind: str) -> bool:
        return self.kinds is None or kind in self.kinds


def config_from_env(env: Mapping[str, str] | None = None) -> EmailConfig | None:
    """Build an :class:`EmailConfig` from the environment, or ``None`` when email alerting is
    not configured (no host, or no recipients). ``None`` is the normal, quiet default — it is
    what keeps an unconfigured machine's behavior byte-identical to before #359.

    Tolerant of a malformed numeric override: a non-numeric port/timeout falls back to the
    default rather than raising, because this is called from inside an alerting path.
    """
    env = os.environ if env is None else env
    host = (env.get("ORCHESTRATOR_SMTP_HOST") or "").strip()
    recipients = _csv(env.get("ORCHESTRATOR_NOTIFY_EMAIL_TO"))
    if not host or not recipients:
        return None

    use_ssl = _flag(env.get("ORCHESTRATOR_SMTP_SSL"), default=False)
    try:
        port = int((env.get("ORCHESTRATOR_SMTP_PORT") or "").strip())
    except ValueError:
        port = _DEFAULT_PORT_SSL if use_ssl else _DEFAULT_PORT_PLAIN
    try:
        timeout_s = float((env.get("ORCHESTRATOR_SMTP_TIMEOUT_S") or "").strip())
    except ValueError:
        timeout_s = _DEFAULT_TIMEOUT_S

    username = (env.get("ORCHESTRATOR_SMTP_USER") or "").strip() or None
    kinds = _csv(env.get("ORCHESTRATOR_NOTIFY_EMAIL_KINDS"))
    return EmailConfig(
        host=host,
        port=port,
        recipients=recipients,
        sender=(env.get("ORCHESTRATOR_SMTP_FROM") or "").strip() or username or _DEFAULT_SENDER,
        username=username,
        password=env.get("ORCHESTRATOR_SMTP_PASSWORD") or None,
        use_ssl=use_ssl,
        # STARTTLS is meaningless on an already-implicit-TLS connection.
        starttls=(not use_ssl) and _flag(env.get("ORCHESTRATOR_SMTP_STARTTLS"), default=True),
        timeout_s=timeout_s,
        kinds=frozenset(kinds) if kinds else None,
    )


# --- rendering (pure) ---------------------------------------------------------------


def _money(cost: object) -> str | None:
    """Render the payload's ``cost`` block. #319: an unmetered call makes the total a FLOOR,
    not a complete figure, so say so rather than printing a confident dollar amount."""
    if not isinstance(cost, dict):
        return None
    usd = cost.get("usd")
    if not isinstance(usd, (int, float)):
        return None
    line = f"${usd:.4f} over {cost.get('invocations', 0)} model call(s)"
    unmetered = cost.get("unmetered_calls") or 0
    if unmetered:
        line += f" — AT LEAST: {unmetered} call(s) had unrecoverable usage and counted as $0"
    return line


def _stage_lines(stages: object) -> list[str]:
    """One line per stage that RAN: status, attempt count, model, and any error tail."""
    if not isinstance(stages, list):
        return []
    lines = []
    for rec in stages:
        if not isinstance(rec, dict):
            continue
        parts = [f"  - {rec.get('stage')}: {rec.get('status')}"]
        if (attempt := rec.get("attempt")):
            parts.append(f"attempt {attempt}")
        if model := rec.get("model"):
            parts.append(str(model))
        line = " | ".join(parts)
        if error := rec.get("error"):
            line += f"\n      error: {str(error)[:300]}"
        lines.append(line)
    return lines


def _action_lines(actions: object) -> list[str]:
    """The release commands on a human-gate alert (#409): one labelled block per
    disposition, rendered so the command can be copied straight out of the mail."""
    if not isinstance(actions, list):
        return []
    lines = []
    for item in actions:
        if not isinstance(item, dict) or not item.get("command"):
            continue
        if label := item.get("label"):
            lines.append(f"  {label}")
        lines.append(f"    {item['command']}")
    return lines


def render_subject(kind: str, payload: Mapping[str, object]) -> str:
    """Subject line: the verdict, the task, and the PR number when there is one — so the
    inbox list alone answers "what happened to which task".

    A human-gate park is the one kind the inbox must not merely record: the run is stalled
    until someone acts, so its subject leads with ACTION NEEDED (#409) — the difference
    between a digest line and a request."""
    task_id = payload.get("task_id")
    bits = [str(task_id)] if task_id else [str(payload.get("run_id") or "")]
    if title := payload.get("title"):
        bits.append(str(title))
    prefix = "[orchestrator] ACTION NEEDED" if kind == _KIND_TASK_BLOCKED else "[orchestrator]"
    subject = f"{prefix} {kind} — {' — '.join(b for b in bits if b)}"
    if (pr := payload.get("pr_number")) is not None:
        subject += f" (PR #{pr})"
    return subject.replace("\n", " ").replace("\r", " ")[:200]


def render_body(kind: str, payload: Mapping[str, object]) -> str:
    """Deterministic plain-text body for any notification payload.

    Every section is optional — the engine's enrichment blocks are best-effort, and the
    poll-driven / run-level kinds carry a thinner payload — so this renders whatever is
    present and never assumes a key exists.
    """
    lines: list[str] = [str(payload.get("summary") or kind), ""]

    facts: list[tuple[str, object]] = [
        ("Run", payload.get("run_id")),
        ("Task", payload.get("task_id")),
        ("Title", payload.get("title")),
        ("Issue", payload.get("issue_number")),
        # Distinct from "Issue" above: a park payload carries BOTH the number and the
        # link, and two lines labelled the same are unreadable without already knowing.
        ("Issue link", payload.get("issue_url")),
        ("State", payload.get("task_state") or payload.get("state")),
        ("Stage", payload.get("stage")),
        ("Held before", payload.get("hold_before")),
        ("Gate", payload.get("gate")),
        ("Reason", payload.get("reason")),
        ("PR", payload.get("pr_url")),
        ("Review approved", payload.get("review_approved")),
        ("Review cycles", payload.get("review_cycles")),
        ("Follow-ups filed", payload.get("followups_filed")),
        ("Improvement", payload.get("improvement_ref")),
    ]
    lines += [f"{label}: {value}" for label, value in facts if value is not None]

    # The human-gate block (#409) goes ABOVE the cost/stage detail: the recipient of a
    # park alert needs the command first, and everything below it is context.
    if action_lines := _action_lines(payload.get("actions")):
        lines += ["", "ACTION NEEDED — this run is parked until you release it:", *action_lines]

    if cost := _money(payload.get("cost")):
        lines.append(f"Cost: {cost}")
    if run_dir := payload.get("run_dir"):
        lines += ["", f"Full trail: {run_dir}"]

    if stage_lines := _stage_lines(payload.get("stages")):
        lines += ["", "Stages:", *stage_lines]

    # The per-task roster on a run_finalized digest.
    roster = payload.get("tasks")
    if isinstance(roster, list) and roster:
        lines += ["", "Tasks:"]
        for entry in roster:
            if not isinstance(entry, dict):
                continue
            row = f"  - {entry.get('task_id')}: {entry.get('state')}"
            if title := entry.get("title"):
                row += f" — {title}"
            if pr_url := entry.get("pr_url"):
                row += f" ({pr_url})"
            lines.append(row)

    # The "what was done" prose — the completion note the engine already published to the
    # PR, reused verbatim rather than re-authored.
    if note := payload.get("note_md"):
        lines += ["", "-" * 60, "", str(note)]

    body = "\n".join(lines)
    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS] + "\n\n… [truncated]"
    return body


def build_message(cfg: EmailConfig, kind: str, payload: Mapping[str, object]) -> EmailMessage:
    """Render a payload into a ready-to-send message."""
    msg = EmailMessage()
    msg["Subject"] = render_subject(kind, payload)
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.recipients)
    # Lets a mail client thread a run's alerts together without parsing the subject.
    if run_id := payload.get("run_id"):
        msg["X-Orchestrator-Run"] = str(run_id)
    msg["X-Orchestrator-Kind"] = kind
    msg.set_content(render_body(kind, payload))
    return msg


# --- delivery ------------------------------------------------------------------------


def smtp_transport(cfg: EmailConfig, msg: EmailMessage) -> None:
    """Default transport: a single short-lived SMTP connection, always with an explicit
    ``timeout`` so a black-holed server cannot wedge the caller."""
    factory = smtplib.SMTP_SSL if cfg.use_ssl else smtplib.SMTP
    with factory(cfg.host, cfg.port, timeout=cfg.timeout_s) as client:
        if cfg.starttls:
            client.starttls()
        if cfg.username and cfg.password:
            client.login(cfg.username, cfg.password)
        client.send_message(msg)


class EmailSink:
    """Callable that mails a notification payload. NEVER raises and NEVER blocks for long."""

    def __init__(self, config: EmailConfig, transport: Transport = smtp_transport) -> None:
        self.config = config
        self.transport = transport

    def __call__(self, kind: str, payload: Mapping[str, object]) -> bool:
        """Send one alert. Returns True if it was delivered, False if it was filtered out by
        the kind allowlist or the send failed. Swallows everything: an alert sink must never
        break a run, and the caller is inside a terminal transition that cannot be replayed."""
        if not self.config.wants(kind):
            return False
        try:
            self.transport(self.config, build_message(self.config, kind, payload))
        except Exception:  # noqa: BLE001 - an alert sink must never break the run
            return False
        return True


def email_sink_from_env(
    env: Mapping[str, str] | None = None, transport: Transport = smtp_transport
) -> EmailSink | None:
    """The adapter entry point: an :class:`EmailSink` when the environment configures one,
    else ``None``. Resolved per call rather than cached, so it stays stateless and a config
    change takes effect without rebuilding the adapter."""
    cfg = config_from_env(env)
    return EmailSink(cfg, transport) if cfg else None
