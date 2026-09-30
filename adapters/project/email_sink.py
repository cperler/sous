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

What a mail says (#524). Each fact appears once, decision first:

* The SUBJECT carries the outcome, the task id, its title and the PR number
  (``COMPLETED #524 — <title> (PR #530)``, ``FAILED at implement …``, ``ACTION NEEDED: parked
  at deliver …``, ``RUN COMPLETED <run> — 3 completed, 1 failed of 4 task(s)``). The body
  does not restate them.
* A per-task body opens with what to do (merge link, retry guidance, or the release
  commands), then the ISSUE (link, labels, an excerpt of the ask and its acceptance
  criteria), then the PULL REQUEST (status, CI, diffstat, files, commits), then the review,
  per-stage models/tokens/cost and the follow-ups filed, then the run trail.
* When the payload carries the completion note, that note IS the review/stage/cost/
  follow-up section: it is embedded verbatim (minus its header lines, which the subject and
  the "Next" line already show) rather than re-rendered, so the PR comment and the mail
  cannot drift. Without it (a failure, a park, a degraded payload) those sections are
  rendered from the payload's ``review``/``stages``/``cost``/``followups`` blocks.
* ``run_finalized`` is the per-run digest: duration, total cost, the integration gate, and
  one entry per task with its issue and PR links. Per-task mail still goes out as tasks
  land; narrow it with the kind allowlist to get only the digest.

Both message parts render from ONE list of sections (``build_sections``): the plain-text
part is complete on its own and the HTML alternative is the same content, formatted, so
the two cannot disagree. Every section is optional, because the engine's enrichment is
best-effort.

#409 asked whether a run reaching PAUSED or exiting ``blocked_on_orphaned_dispatches``
should share this transport: they already do, as ``run_paused`` and ``run_blocked``, so
that class of "a human must act" event needed no new kind — only ``task_blocked`` gained
the ACTION-NEEDED subject and the ``actions`` block, because it is the one whose release
is a single specific command against a specific task. The park alert also closes its own
thread: releasing a task produces the ordinary ``task_completed`` / ``task_failed`` /
``run_finalized`` mail for whatever it goes on to do.
"""

from __future__ import annotations

import html
import os
import re
import smtplib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage

from orchestrator.alerting import NOTIFY_RUN_FINALIZED as _KIND_RUN_FINALIZED
from orchestrator.alerting import NOTIFY_TASK_BLOCKED as _KIND_TASK_BLOCKED
from orchestrator.alerting import NOTIFY_TASK_COMPLETED as _KIND_TASK_COMPLETED
from orchestrator.alerting import NOTIFY_TASK_FAILED as _KIND_TASK_FAILED

# Envelope/format constants.
_DEFAULT_TIMEOUT_S = 10.0
_DEFAULT_PORT_SSL = 465
_DEFAULT_PORT_PLAIN = 587
_DEFAULT_SENDER = "orchestrator@localhost"
# The engine already bounds the prose it folds into a payload; this is the sink's own
# backstop so a hand-built or future payload can't produce a multi-megabyte message.
_MAX_BODY_CHARS = 60_000

Transport = Callable[["EmailConfig", EmailMessage], None]

# The per-task kinds that get the full layout (next action, issue, PR, review, cost, trail).
_TASK_KINDS = frozenset({_KIND_TASK_COMPLETED, _KIND_TASK_FAILED, _KIND_TASK_BLOCKED})


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
#
# Every mail is built ONCE as a list of ``Section``s and then rendered twice, to plain text
# and to HTML, so the two parts cannot say different things (#524). The plain-text part is
# complete on its own; HTML is a presentation of the same sections, never extra content.


@dataclass(frozen=True)
class Link:
    """A URL. Plain text prints the URL itself; HTML may show ``label`` as the anchor text
    (the URL is still the fact, so it is shown exactly once either way)."""

    href: str
    label: str | None = None


Inline = str | Link


@dataclass(frozen=True)
class Para:
    """One line (or pre-wrapped multi-line block) of prose made of inline pieces."""

    parts: tuple[Inline, ...]


@dataclass(frozen=True)
class Bullets:
    items: tuple[tuple[Inline, ...], ...]
    label: str | None = None


@dataclass(frozen=True)
class Table:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    # Column indexes rendered right-aligned (numbers).
    right: frozenset[int] = frozenset()


@dataclass(frozen=True)
class Commands:
    """Copy-pasteable ``(label, command)`` pairs — the release lines on a park alert."""

    items: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Markdown:
    """Markdown passed through verbatim in text and converted in HTML (the completion
    note)."""

    text: str


Block = Para | Bullets | Table | Commands | Markdown


@dataclass(frozen=True)
class Section:
    title: str | None
    blocks: tuple[Block, ...]


def _para(*parts: Inline) -> Para:
    return Para(tuple(p for p in parts if p != ""))


def _field(label: str, value: object) -> Para | None:
    if value is None or value == "" or value == []:
        return None
    return _para(f"{label}: ", value if isinstance(value, Link) else str(value))


def _sec(title: str | None, *blocks: Block | None) -> Section | None:
    kept = tuple(b for b in blocks if b is not None)
    return Section(title, kept) if kept else None


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _url(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


# --- facts → text fragments -----------------------------------------------------------


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


def _money_cell(cost: object) -> str | None:
    """A cost block as one short figure for a list entry: ``≥$X`` when some calls were
    unmetered (a floor), ``n/a (unmetered)`` when all were, else the plain figure (#319)."""
    if not isinstance(cost, dict) or not isinstance(cost.get("usd"), (int, float)):
        return None
    calls = int(cost.get("invocations") or 0)
    unmetered = int(cost.get("unmetered_calls") or 0)
    if unmetered and unmetered >= calls:
        return "n/a (unmetered)"
    return f"{'≥' if unmetered else ''}${cost['usd']:.4f}"


def _tokens(value: object) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _stage_cost(rec: dict) -> str:
    """Per-stage cost cell, with the same honesty rules as the completion note: the ENGINE
    lane's $0 is a real measurement, an interactive or unmetered stage's is an unknown."""
    if rec.get("lane") == "engine":
        return "$0 (engine)"
    if rec.get("lane") == "interactive":
        return "n/a"
    if rec.get("metered") is False:
        return "n/a (unmetered)"
    cost = rec.get("cost_usd")
    return f"${cost:.4f}" if isinstance(cost, (int, float)) else "—"


def _duration(seconds: object) -> str:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return "—"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _diffstat(pr: dict) -> str | None:
    adds, dels, files = pr.get("additions"), pr.get("deletions"), pr.get("changed_files")
    if not isinstance(adds, int) or not isinstance(dels, int):
        return None
    text = f"+{adds} −{dels}"
    if isinstance(files, int):
        text += f" across {files} file(s)"
    commits = len(_list(pr.get("commits"))) + int(pr.get("commits_omitted") or 0)
    if commits:
        text += f", {commits} commit(s)"
    return text


# The completion note's header repeats what the mail already shows above it (the subject
# names the task, the "Next" line links the PR), so those lines are dropped when the note
# is embedded. Everything from the review verdict down is kept verbatim.
_NOTE_HEADER = re.compile(r"^(##\s+Orchestration run complete\b.*|-\s+\*\*(Task|PR):\*\*.*)$")


def _trim_note(note: str) -> str:
    lines = note.splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or _NOTE_HEADER.match(lines[i])):
        i += 1
    return "\n".join(lines[i:]).strip()


# --- sections ---------------------------------------------------------------------


def _next_blocks(kind: str, p: Mapping[str, object]) -> list[Block | None]:
    """The decision-relevant lines that open a task mail: what, if anything, to do."""
    pr = _dict(p.get("pr"))
    url = _url(pr.get("url")) or _url(p.get("pr_url"))
    if kind == _KIND_TASK_COMPLETED:
        if not url:
            return [_para("Next: nothing to merge; this task opened no PR.")]
        if str(pr.get("state") or "").upper() == "MERGED":
            return [_para("Next: nothing; the PR is already merged: ", Link(url))]
        if pr.get("checks") == "failure":
            return [_para("Next: CI is failing on the PR. Check it, then merge: ", Link(url))]
        if pr.get("draft") is True:
            return [_para("Next: the PR is a draft. Mark it ready, then merge: ", Link(url))]
        return [_para("Next: review and merge ", Link(url))]
    if kind == _KIND_TASK_FAILED:
        return [
            _field("Reason", p.get("reason")),
            _para("Next: read the stage logs in the run trail below, then retry or "
                  "abandon the task."),
        ]
    if kind == _KIND_TASK_BLOCKED:
        commands = tuple(
            (str(item.get("label") or ""), str(item["command"]))
            for item in _list(p.get("actions"))
            if isinstance(item, dict) and item.get("command")
        )
        blocks: list[Block | None] = [
            _field("Held before", p.get("hold_before")),
            None if p.get("hold_before") else _field("Stage", p.get("stage")),
            _field("Gate", p.get("gate")),
            _field("Reason", p.get("reason")),
        ]
        if commands:
            blocks += [_para("ACTION NEEDED — this run is parked until you release it:"),
                       Commands(commands)]
        return blocks
    return []


def _issue_section(p: Mapping[str, object]) -> Section | None:
    url = _url(p.get("issue_url"))
    number = p.get("issue_number")
    link: Inline | None = None
    if url:
        link = Link(url, f"#{number}" if number is not None else None)
    elif number is not None and str(p.get("task_id")) != f"#{number}":
        link = f"#{number}"  # only when the subject's task id does not already say it
    labels = [str(label) for label in _list(p.get("labels")) if str(label).strip()]
    excerpt = p.get("issue_excerpt")
    acceptance = p.get("issue_acceptance")
    return _sec(
        "Issue",
        _field("Link", link) if link is not None else None,
        _field("Labels", ", ".join(labels)) if labels else None,
        _para(str(excerpt)) if isinstance(excerpt, str) and excerpt.strip() else None,
        _para("Acceptance:\n", str(acceptance))
        if isinstance(acceptance, str) and acceptance.strip() else None,
    )


def _pr_section(kind: str, p: Mapping[str, object]) -> Section | None:
    pr = _dict(p.get("pr"))
    url = _url(pr.get("url")) or _url(p.get("pr_url"))
    # The completed mail already links the PR on its "Next" line; say it once.
    link = _field("Link", Link(url)) if url and kind != _KIND_TASK_COMPLETED else None
    if not pr:
        return _sec("Pull request", link)
    title = pr.get("title")
    status_bits = []
    if state := pr.get("state"):
        status_bits.append(str(state).lower() + (" draft" if pr.get("draft") is True else ""))
    status_bits.append(f"CI {pr.get('checks') or 'unknown'}")
    if decision := pr.get("review_decision"):
        status_bits.append(f"GitHub review {str(decision).lower().replace('_', ' ')}")
    files = [f for f in _list(pr.get("files")) if isinstance(f, dict)]
    commits = [c for c in _list(pr.get("commits")) if isinstance(c, dict)]
    blocks: list[Block | None] = [
        link,
        _field("Title", title) if title and title != p.get("title") else None,
        _field("Status", ", ".join(status_bits)),
        _field("Diff", _diffstat(pr)),
    ]
    if files:
        blocks.append(Table(
            ("+", "−", "File"),
            tuple((str(f.get("additions", "")), str(f.get("deletions", "")),
                   str(f.get("path") or "")) for f in files),
            right=frozenset({0, 1}),
        ))
        if omitted := pr.get("files_omitted"):
            blocks.append(_para(f"… and {omitted} more file(s)"))
    if commits:
        items = tuple(
            (f"{c.get('sha') or ''} {c.get('title') or ''}".strip(),) for c in commits
        )
        if omitted := pr.get("commits_omitted"):
            items += ((f"… and {omitted} more commit(s)",),)
        blocks.append(Bullets(items, label="Commits"))
    return _sec("Pull request", *blocks)


def _review_section(p: Mapping[str, object]) -> Section | None:
    """Review outcome, for a mail WITHOUT the completion note (the note carries it)."""
    review = _dict(p.get("review"))
    approved = review.get("approved", p.get("review_approved"))
    verdict = ("approved" if approved is True
               else "changes requested" if approved is False else None)
    blocking = [str(b) for b in _list(review.get("blocking")) if b]
    if omitted := review.get("blocking_omitted"):
        blocking.append(f"… and {omitted} more")
    non_blocking = [
        f"{f.get('title')}" + (f" ({f['disposition']})" if f.get("disposition") else "")
        for f in _list(review.get("non_blocking")) if isinstance(f, dict) and f.get("title")
    ]
    if omitted := review.get("non_blocking_omitted"):
        non_blocking.append(f"… and {omitted} more")
    return _sec(
        "Review",
        _field("Verdict", verdict),
        Bullets(tuple((b,) for b in blocking), label="Blocking") if blocking else None,
        Bullets(tuple((n,) for n in non_blocking), label="Non-blocking")
        if non_blocking else None,
    )


def _stages_section(p: Mapping[str, object]) -> Section | None:
    """Per-stage model/tokens/cost/time plus the task total, for a mail WITHOUT the
    completion note (the note carries its own stage table and total)."""
    stages = [s for s in _list(p.get("stages")) if isinstance(s, dict)]
    table = None
    if stages:
        table = Table(
            ("Stage", "Status", "Model", "Effort", "In", "Out", "Cost", "Time"),
            tuple(
                (str(s.get("stage") or ""), str(s.get("status") or ""),
                 str(s.get("model") or "—"), str(s.get("effort") or "—"),
                 _tokens(s.get("input_tokens")), _tokens(s.get("output_tokens")),
                 _stage_cost(s), _duration(s.get("duration_s")))
                for s in stages
            ),
            right=frozenset({4, 5, 6, 7}),
        )
    # A failure mail already leads with its reason; the stage error that IS that reason
    # is not listed a second time.
    reason = str(p.get("reason") or "").strip()
    errors = tuple(
        (f"{s.get('stage')}: {str(s['error'])[:300]}",)
        for s in stages if s.get("error") and str(s["error"]).strip() != reason
    )
    money = _money(p.get("cost"))
    return _sec(
        "Stages and cost",
        table,
        Bullets(errors, label="Errors") if errors else None,
        _field("Total", money),
    )


def _followups_section(p: Mapping[str, object]) -> Section | None:
    """What this task filed, for a mail WITHOUT the completion note."""
    items: list[tuple[Inline, ...]] = []
    for f in _list(p.get("followups")):
        if not isinstance(f, dict):
            continue
        ref = _url(f.get("ref"))
        title = str(f.get("title") or "(untitled)")
        items.append((f"{title}: ", Link(ref)) if ref else (f"{title}: (filing failed)",))
    if not items and (count := p.get("followups_filed")):
        items.append((f"{count} follow-up(s) filed",))
    if ref := _url(p.get("improvement_ref")):
        items.append(("Improvement idea: ", Link(ref)))
    for ref in _list(p.get("optimize_suggestion_refs")):
        if _url(ref):
            items.append(("Architectural suggestion: ", Link(ref)))
    return _sec("Follow-ups filed", Bullets(tuple(items)) if items else None)


def _trail_section(p: Mapping[str, object]) -> Section | None:
    bits = []
    if run_id := p.get("run_id"):
        bits.append(f"Run {run_id}")
    attempt = p.get("attempt")
    if isinstance(attempt, int) and attempt > 1:
        bits.append(f"attempt {attempt}")
    cycles = p.get("review_cycles")
    if isinstance(cycles, int) and cycles:
        bits.append(f"{cycles} review cycle(s)")
    return _sec(
        "Trail",
        _para(", ".join(bits) + ".") if bits else None,
        _field("Full trail", p.get("run_dir")),
    )


def _task_sections(kind: str, p: Mapping[str, object]) -> list[Section | None]:
    """task_completed / task_failed / task_blocked: decision first, then the ask, then
    what changed, then how it went, then where the trail is."""
    sections = [
        _sec(None, *_next_blocks(kind, p)),
        _issue_section(p),
        _pr_section(kind, p),
    ]
    note = p.get("note_md")
    if isinstance(note, str) and note.strip():
        # The completion note already published to the PR is the single source for the
        # review verdict, stage table, total, and findings — embedded, not re-rendered.
        sections.append(_sec("Review, stages and cost", Markdown(_trim_note(note))))
    else:
        sections += [_review_section(p), _stages_section(p), _followups_section(p)]
    sections.append(_trail_section(p))
    return sections


def _gate_line(gate: object) -> Para | None:
    gate = _dict(gate)
    if not gate:
        return None
    if gate.get("green"):
        return _para("Integration gate: green")
    failing = ", ".join(str(f) for f in _list(gate.get("failing"))) or "unknown"
    text = f"Integration gate: RED (failing: {failing})"
    filed = gate.get("filed")
    if isinstance(filed, str) and filed.startswith("http"):
        return _para(text + ", filed ", Link(filed))
    return _para(text + (f", filed {filed}" if filed else ""))


def _run_sections(p: Mapping[str, object]) -> list[Section | None]:
    """run_finalized: the batch digest — headline numbers, then one entry per task."""
    roster = [t for t in _list(p.get("tasks")) if isinstance(t, dict)]
    ready = [t for t in roster if t.get("state") == "completed" and _url(t.get("pr_url"))]
    head: list[Block | None] = [
        _field("Duration", _duration(p.get("duration_s")) if "duration_s" in p else None),
        _field("Cost", _money(p.get("cost"))),
        _gate_line(p.get("integration_gate")),
        _para(f"Next: review and merge the {len(ready)} PR(s) below.") if ready else None,
    ]
    entries: list[tuple[Inline, ...]] = []
    for t in roster:
        line: list[Inline] = [f"{t.get('task_id')} {t.get('state')}"]
        if title := t.get("title"):
            line.append(f" — {title}")
        if cost := _money_cell(t.get("cost")):
            line.append(f" — {cost}")
        if url := _url(t.get("issue_url")):
            line += ["\n  issue ", Link(url)]
        if url := _url(t.get("pr_url")):
            line += ["\n  PR ", Link(url)]
        entries.append(tuple(line))
    return [
        _sec(None, *head),
        _sec("Tasks", Bullets(tuple(entries))) if entries else None,
        _sec("Trail", _field("Full trail", p.get("run_dir"))),
    ]


def _generic_sections(kind: str, p: Mapping[str, object]) -> list[Section | None]:
    """Every other kind (stale, paused, blocked run, budget warning, superseded): the
    summary line plus whatever specifics the payload carries."""
    facts = [
        _field("Run", p.get("run_id")),
        _field("Task", p.get("task_id")),
        _field("Title", p.get("title")),
        _field("State", p.get("task_state") or p.get("state")),
        _field("Stage", p.get("stage")),
        _field("Reason", p.get("reason")),
        _field("PR", Link(url)) if (url := _url(p.get("pr_url"))) else None,
        _field("Cost", _money(p.get("cost"))),
    ]
    return [
        _sec(None, _para(str(p.get("summary") or kind))),
        _sec(None, *facts),
        _stages_section(p) if p.get("stages") else None,
        _sec("Trail", _field("Full trail", p.get("run_dir"))),
    ]


def build_sections(kind: str, payload: Mapping[str, object]) -> list[Section]:
    """The one content model both message parts render from. Every section is optional —
    the engine's enrichment blocks are best-effort and the poll-driven kinds carry a thin
    payload — so a builder never assumes a key exists or has the right shape."""
    if kind == _KIND_RUN_FINALIZED:
        sections = _run_sections(payload)
    elif kind in _TASK_KINDS:
        sections = _task_sections(kind, payload)
    else:
        sections = _generic_sections(kind, payload)
    return [s for s in sections if s is not None]


# --- subject ------------------------------------------------------------------------


def render_subject(kind: str, payload: Mapping[str, object]) -> str:
    """Subject line: the outcome, the task, its title, and the PR number when there is one,
    so the inbox list alone answers "what happened to which task". The body never repeats
    these.

    A human-gate park is the one kind the inbox must not merely record: the run is stalled
    until someone acts, so its subject leads with ACTION NEEDED (#409). The raw ``kind`` is
    in the ``X-Orchestrator-Kind`` header for filtering."""
    task_id = payload.get("task_id")
    title = payload.get("title")
    task = str(task_id or "task")
    tail = f" — {title}" if title else ""
    if kind == _KIND_TASK_COMPLETED:
        subject = f"[orchestrator] COMPLETED {task}{tail}"
    elif kind == _KIND_TASK_FAILED:
        stage = payload.get("stage")
        subject = f"[orchestrator] FAILED {task}{f' at {stage}' if stage else ''}{tail}"
    elif kind == _KIND_TASK_BLOCKED:
        if held := payload.get("hold_before"):
            where = f" before {held}"
        elif stage := payload.get("stage"):
            where = f" at {stage}"
        else:
            where = ""
        subject = f"[orchestrator] ACTION NEEDED: {task} parked{where}{tail}"
    elif kind == _KIND_RUN_FINALIZED:
        subject = f"[orchestrator] RUN {_run_headline(payload)}"
    else:
        bits = [str(task_id)] if task_id else [str(payload.get("run_id") or "")]
        if title:
            bits.append(str(title))
        subject = f"[orchestrator] {kind} — {' — '.join(b for b in bits if b)}"
    if kind in _TASK_KINDS and (pr := payload.get("pr_number")) is not None:
        subject += f" (PR #{pr})"
    return subject.replace("\n", " ").replace("\r", " ").rstrip()[:200]


def _run_headline(p: Mapping[str, object]) -> str:
    state = str(p.get("state") or "finalized").replace("_", " ").upper()
    head = f"{state} {p.get('run_id') or ''}".rstrip()
    counts = _dict(p.get("counts"))
    total = counts.get("total")
    if not isinstance(total, int):
        roster = _list(p.get("tasks"))
        if not roster:
            return head
        total = len(roster)
        counts = {}
        for t in roster:
            if isinstance(t, dict):
                counts[str(t.get("state"))] = counts.get(str(t.get("state")), 0) + 1
    parts = [f"{n} {state_name.replace('_', ' ')}" for state_name, n in counts.items()
             if state_name != "total" and n]
    return f"{head} — {', '.join(parts)} of {total} task(s)" if parts else head


# --- plain text -----------------------------------------------------------------------


def _inline_text(parts: tuple[Inline, ...]) -> str:
    return "".join(p.href if isinstance(p, Link) else p for p in parts)


def _table_text(table: Table) -> list[str]:
    rows = [table.headers, *table.rows]
    widths = [max(len(r[i]) for r in rows) for i in range(len(table.headers))]

    def fmt(row: tuple[str, ...]) -> str:
        cells = [c.rjust(w) if i in table.right else c.ljust(w)
                 for i, (c, w) in enumerate(zip(row, widths, strict=False))]
        return "  ".join(cells).rstrip()

    return [fmt(table.headers), "  ".join("-" * w for w in widths), *map(fmt, table.rows)]


def _block_text(block: Block) -> list[str]:
    if isinstance(block, Para):
        return [_inline_text(block.parts)]
    if isinstance(block, Bullets):
        head = [f"{block.label}:"] if block.label else []
        return head + [f"- {_inline_text(item)}" for item in block.items]
    if isinstance(block, Table):
        return _table_text(block)
    if isinstance(block, Commands):
        out = []
        for label, command in block.items:
            if label:
                out.append(f"  {label}")
            out.append(f"    {command}")
        return out
    return [block.text]


def _text(sections: list[Section]) -> str:
    chunks = []
    for section in sections:
        lines = [section.title.upper()] if section.title else []
        for block in section.blocks:
            lines += _block_text(block)
        chunks.append("\n".join(lines))
    return "\n\n".join(chunks)


def render_body(kind: str, payload: Mapping[str, object]) -> str:
    """Deterministic plain-text body. Complete on its own: the HTML part adds formatting,
    never facts."""
    body = _text(build_sections(kind, payload))
    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS] + "\n\n… [truncated]"
    return body


# --- HTML -------------------------------------------------------------------------------

_STYLE_BODY = ("font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;"
               "font-size:14px;line-height:1.45;color:#1f2328;max-width:860px")
_STYLE_H = "font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#57606a;" \
           "margin:20px 0 6px;border-bottom:1px solid #d0d7de;padding-bottom:2px"
_STYLE_TABLE = "border-collapse:collapse;font-size:13px;margin:6px 0"
_STYLE_CELL = "border:1px solid #d0d7de;padding:3px 8px"
_STYLE_PRE = "background:#f6f8fa;padding:6px 10px;margin:2px 0 8px;font-size:13px"

_URL_RE = re.compile(r"https?://[^\s<>()\"']+[^\s<>()\"'.,;:!?]")
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_BULLET_RE = re.compile(r"^[-*]\s+")
_ITALIC_RE = re.compile(r"(?<![\w*])_(.+?)_(?!\w)")


def _a(href: str, label: str | None = None) -> str:
    return f'<a href="{html.escape(href, quote=True)}">{html.escape(label or href)}</a>'


def _inline_html(parts: tuple[Inline, ...]) -> str:
    out = []
    for part in parts:
        if isinstance(part, Link):
            out.append(_a(part.href, part.label))
        else:
            out.append(html.escape(part).replace("\n", "<br>"))
    return "".join(out)


def _md_inline(text: str) -> str:
    """Escape, then apply the small markdown subset the completion note uses: code spans,
    bold, italic, and bare URLs as links. Code spans are split out first so nothing inside
    them is reformatted."""
    pieces = _CODE_RE.split(text)
    out = []
    for i, piece in enumerate(pieces):
        if i % 2:
            out.append(f"<code>{html.escape(piece)}</code>")
            continue
        esc = html.escape(piece, quote=False)
        esc = _URL_RE.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', esc)
        esc = _BOLD_RE.sub(r"<strong>\1</strong>", esc)
        esc = _ITALIC_RE.sub(r"<em>\1</em>", esc)
        out.append(esc)
    return "".join(out)


def _md_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def markdown_to_html(md: str) -> str:
    """Minimal markdown → HTML for the completion note: headings, bullet lists, pipe
    tables, paragraphs, and the inline subset above. Everything is escaped, so a note can
    never inject markup into the mail."""
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if heading := re.match(r"^(#{1,6})\s+(.*)$", stripped):
            out.append(f"<h4 style=\"margin:14px 0 4px\">{_md_inline(heading.group(2))}</h4>")
            i += 1
        elif stripped.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = _md_cells(lines[i])
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                style = f"{_STYLE_CELL};text-align:left"
                head = "".join(f'<th style="{style}">{_md_inline(c)}</th>' for c in rows[0])
                body = "".join(
                    "<tr>" + "".join(f'<td style="{style}">{_md_inline(c)}</td>'
                                     for c in row) + "</tr>"
                    for row in rows[1:]
                )
                out.append(f'<table style="{_STYLE_TABLE}"><tr>{head}</tr>{body}</table>')
        elif _BULLET_RE.match(stripped):
            items = []
            while i < len(lines) and _BULLET_RE.match(lines[i].strip()):
                item = _BULLET_RE.sub("", lines[i].strip(), count=1)
                items.append(f"<li>{_md_inline(item)}</li>")
                i += 1
            out.append(f"<ul>{''.join(items)}</ul>")
        else:
            para = []
            while (i < len(lines) and lines[i].strip()
                   and not re.match(r"^(#{1,6}\s|\||[-*]\s)", lines[i].strip())):
                para.append(_md_inline(lines[i].strip()))
                i += 1
            out.append(f"<p>{'<br>'.join(para)}</p>")
    return "\n".join(out)


def _block_html(block: Block) -> str:
    if isinstance(block, Para):
        return f'<p style="margin:4px 0">{_inline_html(block.parts)}</p>'
    if isinstance(block, Bullets):
        head = f'<p style="margin:6px 0 0">{html.escape(block.label)}:</p>' if block.label else ""
        items = "".join(f"<li>{_inline_html(item)}</li>" for item in block.items)
        return f'{head}<ul style="margin:2px 0 8px">{items}</ul>'
    if isinstance(block, Table):
        def cell(tag: str, text: str, idx: int) -> str:
            align = "right" if idx in block.right else "left"
            return (f'<{tag} style="{_STYLE_CELL};text-align:{align}">'
                    f"{html.escape(text)}</{tag}>")

        head = "".join(cell("th", h, i) for i, h in enumerate(block.headers))
        rows = "".join(
            "<tr>" + "".join(cell("td", c, i) for i, c in enumerate(row)) + "</tr>"
            for row in block.rows
        )
        return f'<table style="{_STYLE_TABLE}"><tr>{head}</tr>{rows}</table>'
    if isinstance(block, Commands):
        return "".join(
            (f'<p style="margin:6px 0 0">{html.escape(label)}</p>' if label else "")
            + f'<pre style="{_STYLE_PRE}">{html.escape(command)}</pre>'
            for label, command in block.items
        )
    return markdown_to_html(block.text)


def render_html(kind: str, payload: Mapping[str, object]) -> str:
    """The HTML alternative: the same sections as :func:`render_body`, formatted. If the
    result would exceed the size bound it falls back to the (already bounded) plain text
    in a ``<pre>``, because cutting HTML mid-tag would produce a broken document."""
    parts = []
    for section in build_sections(kind, payload):
        if section.title:
            parts.append(f'<h3 style="{_STYLE_H}">{html.escape(section.title)}</h3>')
        parts += [_block_html(b) for b in section.blocks]
    inner = "\n".join(parts)
    if len(inner) > _MAX_BODY_CHARS * 2:
        inner = f"<pre>{html.escape(render_body(kind, payload))}</pre>"
    return f'<html><body style="{_STYLE_BODY}">\n{inner}\n</body></html>\n'


def build_message(cfg: EmailConfig, kind: str, payload: Mapping[str, object]) -> EmailMessage:
    """Render a payload into a ready-to-send message: a complete plain-text part plus an
    HTML alternative built from the same sections."""
    msg = EmailMessage()
    msg["Subject"] = render_subject(kind, payload)
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.recipients)
    # Lets a mail client thread a run's alerts together without parsing the subject.
    if run_id := payload.get("run_id"):
        msg["X-Orchestrator-Run"] = str(run_id)
    msg["X-Orchestrator-Kind"] = kind
    msg.set_content(render_body(kind, payload))
    msg.add_alternative(render_html(kind, payload), subtype="html")
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
