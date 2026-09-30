"""Read-only web skin over the cross-session board (#94).

The terminal ``orchestrator dashboard`` (#6) already assembles the whole picture: a pure
``dashboard_snapshot(root, ...)`` folds every ``runs/<id>/`` store into an attention-first
board, and ``stream_probe.probe_current_stream`` turns a partially-written stage stream into a
live activity snapshot. This module is the thin HTTP layer that serves those two data functions
as JSON plus a self-contained static page that polls them — no engine changes, read-only, and
still project-agnostic (the engine is never touched for model work; the board only *reads*).

The routing is factored into a PURE ``route_request`` so the whole surface is unit-testable
without a socket: it serves the inlined static page for ``/``, ``dashboard_snapshot`` as JSON for
``/api/snapshot``, ``probe_current_stream`` (over ``root/<run>``) as JSON for ``/api/stream``,
and — for the per-task detail panel (#525) — a task's stage-file listing plus its latest review
findings (``/api/task-detail``) and one stage file's raw text (``/api/stage-file``), confined to
regular files directly under that task's ``stages/<task>/`` dir.
Like the terminal board, it covers one runs-root or SEVERAL (#386), so one local page is the
whole machine's view of what is in flight across projects.
The server wrapper (``build_server`` / ``serve``) is a tiny GET-only handler around it; only the
final ``serve_forever`` loop is not exercised by a unit test.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from .dashboard import EngineFactory, Roots, dashboard_snapshot, resolve_run_root
from .status_store import safe_task_dirname
from .stream_probe import probe_current_stream, stages_dir

_JSON_CT = "application/json; charset=utf-8"
_HTML_CT = "text/html; charset=utf-8"
_TEXT_CT = "text/plain; charset=utf-8"

#: The file names a task's ``stages/<task>/`` dir legitimately holds, and so the only names
#: ``/api/stage-file`` serves: the per-stage record and its Markdown (``NN-<stage>.json|md``),
#: a dispatch's prompt / raw stream / stderr tee (``<stage>-attempt<N>[.<phase>][.retry<K>]``
#: + ``.prompt.txt|.stream.jsonl|.stderr.log``, named by ``stream_probe``), the completion note
#: and the task index. Anything else is refused by name before the filesystem is consulted, so
#: the endpoint cannot be walked into the run's status docs or ledger.
_STAGE_FILE_RE = re.compile(
    r"\d{2,}-[a-z_]+\.(?:json|md)"
    r"|[a-z_]+-attempt\d+(?:\.[A-Za-z0-9_-]+)?(?:\.retry\d+)?"
    r"\.(?:prompt\.txt|stream\.jsonl|stderr\.log)"
    r"|completion-note\.md|index\.md"
)
_REVIEW_RECORD_RE = re.compile(r"(\d{2,})-review\.json")


def _json(status: int, obj: object) -> tuple[int, str, bytes]:
    """A (status, content-type, body) JSON triple. ``default=str`` handles an unexpected
    non-serializable value gracefully, degrading it to its ``str`` instead of raising."""
    return status, _JSON_CT, json.dumps(obj, default=str).encode("utf-8")


def _first(query: dict[str, list[str]], key: str) -> str | None:
    """The first value for a parsed query-string key, or ``None`` (missing/empty)."""
    vals = query.get(key)
    return vals[0] if vals else None


class _Refused(Exception):
    """A stage-file request refused with an HTTP status and a JSON error body."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


def _task_stage_dir(run_root: Path, task: str) -> Path:
    """The resolved ``stages/<task>/`` dir for ``task``, refusing a task id that would name
    anything other than a direct child of ``stages/``. ``safe_task_dirname`` already folds
    ``/`` away, but ``..`` survives it — so the resolved parent is checked, which also
    catches a task dir that is itself a symlink leading out of ``stages/``."""
    dirname = safe_task_dirname(task)
    if dirname.startswith(".") or "\\" in dirname:
        raise _Refused(400, "invalid task id")
    base = (run_root / "stages").resolve()
    d = stages_dir(run_root, task).resolve()
    if d.parent != base:
        raise _Refused(400, "task dir resolves outside the run's stages dir")
    if not d.is_dir():
        raise _Refused(404, "no stage files for this task")
    return d


def _stage_file_path(run_root: Path, task: str, name: str) -> Path:
    """Where ``/api/stage-file`` may read ``name`` from, or ``_Refused``. Only a KNOWN stage
    file name (``_STAGE_FILE_RE``) that is a regular file directly under the task's stage
    dir is served; a symlink is followed only if it lands back in that same dir."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise _Refused(400, "path traversal refused")
    if not _STAGE_FILE_RE.fullmatch(name):
        raise _Refused(400, "unknown stage file name")
    d = _task_stage_dir(run_root, task)
    path = d / name
    if not path.is_symlink() and not path.exists():
        raise _Refused(404, "no such stage file")
    real = path.resolve()
    if real.parent != d:
        raise _Refused(400, "symlink outside the task's stage dir refused")
    if not real.is_file():
        raise _Refused(404, "not a regular file")
    return real


def _review_findings(d: Path) -> dict | None:
    """The latest REVIEW record's verdict and findings for the detail panel, or None when the
    task has not been reviewed. Read from the newest ``NN-review.json`` (the highest record
    sequence); an unreadable record is reported as such rather than hidden."""
    records = []
    for p in d.iterdir():
        m = _REVIEW_RECORD_RE.fullmatch(p.name)
        if m and p.is_file():
            records.append((int(m.group(1)), p))
    if not records:
        return None
    _, latest = max(records)
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"file": latest.name, "error": f"{type(exc).__name__}: {exc}"}
    # Valid JSON need not be an object (a list, string or number parses too), and neither
    # need ``structured_output`` — treat either as empty rather than 500 the whole panel.
    if not isinstance(payload, dict):
        payload = {}
    out = payload.get("structured_output")
    if not isinstance(out, dict):
        out = {}
    return {
        "file": latest.name,
        "attempt": payload.get("attempt"),
        "approved": out.get("approved"),
        "issues": out.get("issues") or [],
        "non_blocking": out.get("non_blocking") or [],
    }


def _task_detail(run_root: Path, task: str) -> dict:
    """A task's stage files (name + size, only the names ``/api/stage-file`` will serve) and
    its latest review findings — what the collapsible task panel lists and links."""
    d = _task_stage_dir(run_root, task)
    files = []
    for p in sorted(d.iterdir(), key=lambda p: p.name):
        # Only serve known stage-file names that are regular files; check symlink safety.
        if not _STAGE_FILE_RE.fullmatch(p.name) or not p.is_file():
            continue
        if p.resolve().parent != d:  # Symlink leading outside is refused.
            continue
        files.append({"name": p.name, "size": p.stat().st_size})
    return {"files": files, "review": _review_findings(d)}


def route_request(
    path: str,
    query: dict[str, list[str]],
    *,
    root: Roots,
    engine_factory: EngineFactory,
    usage_reader: Callable[[], object] | None = None,
    clock: Callable[[], float] = time.time,
    snap_kwargs: dict | None = None,
) -> tuple[int, str, bytes]:
    """Map one GET (path + parsed query) to a ``(status, content_type, body)`` triple. Pure:
    no socket, no globals — the server handler and the tests both drive exactly this.

    Routes:
      - ``/`` (and ``/index.html``) → the inlined, self-contained static page.
      - ``/api/snapshot`` → ``dashboard_snapshot(root, ...)`` as JSON.
      - ``/api/stream?run=&task=&stage=[&root=]`` → ``probe_current_stream`` for that run as
        JSON, or ``404`` when there is no stream to probe (interactive/ENGINE lane, or nothing
        dispatched yet). The run's root is resolved by discovery across every configured
        runs-root (#386); ``root=`` disambiguates when two roots hold the same run id, and an
        unknown/ambiguous run is a ``404`` rather than a guessed path. Missing ``run``/``task``
        → ``400``.
      - ``/api/task-detail?run=&task=[&root=]`` → that task's servable stage files and its
        latest review findings as JSON (#525).
      - ``/api/stage-file?run=&task=&name=[&root=]`` → one stage file's raw bytes as
        ``text/plain`` (#525). Read-only and confined: ``name`` must be a known stage-file
        name and a regular file directly under ``stages/<task>/`` — traversal, a symlink
        leading out of that dir, and unknown names are ``400``; an unknown run or a missing
        file is ``404``.
      - anything else → ``404``.

    ``snap_kwargs`` is forwarded verbatim to ``dashboard_snapshot``; the same keys the CLI
    accepts (``show_all``, ``limit``, ``stale_after_s``) work here. ``clock`` and
    ``usage_reader`` are injected for deterministic testing — tests freeze ``clock`` and omit
    ``usage_reader``; production callers use the ``time.time`` and ``read_usage`` defaults.
    """
    snap_kwargs = snap_kwargs or {}
    if path in ("/", "/index.html"):
        return 200, _HTML_CT, INDEX_HTML.encode("utf-8")
    if path == "/api/snapshot":
        snap = dashboard_snapshot(
            root,
            engine_factory=engine_factory,
            usage_reader=usage_reader,
            clock=clock,
            **snap_kwargs,
        )
        return _json(200, snap)
    if path == "/api/stream":
        run = _first(query, "run")
        task = _first(query, "task")
        stage = _first(query, "stage")
        if not run or not task:
            return _json(400, {"error": "run and task query params are required"})
        run_root = resolve_run_root(root, run, prefer_root=_first(query, "root"))
        if run_root is None:
            return _json(404, {"error": "unknown or ambiguous run", "run": run, "task": task})
        probe = probe_current_stream(run_root, task, stage)
        if probe is None:
            return _json(404, {"error": "no stream", "run": run, "task": task, "stage": stage})
        return _json(200, {"run": run, "task": task, "stage": stage, **probe})
    if path in ("/api/task-detail", "/api/stage-file"):
        run = _first(query, "run")
        task = _first(query, "task")
        name = _first(query, "name")
        if not run or not task or (path == "/api/stage-file" and not name):
            need = "run and task" if path == "/api/task-detail" else "run, task and name"
            return _json(400, {"error": f"{need} query params are required"})
        run_root = resolve_run_root(root, run, prefer_root=_first(query, "root"))
        if run_root is None:
            return _json(404, {"error": "unknown or ambiguous run", "run": run, "task": task})
        try:
            if path == "/api/task-detail":
                return _json(200, {"run": run, "task": task, **_task_detail(run_root, task)})
            # The 400 guard above guarantees name is set on the stage-file path.
            body = _stage_file_path(run_root, task, cast(str, name)).read_bytes()
        except _Refused as exc:
            return _json(exc.status, {"error": exc.error, "run": run, "task": task, "name": name})
        return 200, _TEXT_CT, body
    return _json(404, {"error": "not found", "path": path})


# --- server wrapper ---------------------------------------------------------------------


def _make_handler(
    *,
    root: Roots,
    engine_factory: EngineFactory,
    usage_reader: Callable[[], object] | None,
    clock: Callable[[], float],
    snap_kwargs: dict | None,
) -> type[BaseHTTPRequestHandler]:
    """A GET-only ``BaseHTTPRequestHandler`` subclass that delegates every request to the pure
    ``route_request``. The read config is captured in the closure so nothing global is needed."""

    class _Handler(BaseHTTPRequestHandler):
        server_version = "orchestrator-dashboard/1"

        def log_message(self, *_args: object) -> None:  # noqa: D401 - quiet the default stderr spam
            """Silence the per-request stderr logging (this is a local read-only viewer)."""

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's required name
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            try:
                status, ctype, body = route_request(
                    parsed.path,
                    query,
                    root=root,
                    engine_factory=engine_factory,
                    usage_reader=usage_reader,
                    clock=clock,
                    snap_kwargs=snap_kwargs,
                )
            except Exception as exc:  # noqa: BLE001 - a probe/assembly miss is a 500, not a crash
                status, ctype, body = _json(500, {"error": str(exc)})
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            # Stage files are model output served as text: never let a browser sniff one
            # into HTML, and never cache a view of a run that is still changing.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler


def build_server(
    root: Roots,
    engine_factory: EngineFactory,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    usage_reader: Callable[[], object] | None = None,
    clock: Callable[[], float] = time.time,
    snap_kwargs: dict | None = None,
) -> ThreadingHTTPServer:
    """Bind (but do not serve) a ``ThreadingHTTPServer`` wired to ``route_request``. ``port=0``
    binds an ephemeral port — the caller reads ``server.server_address[1]`` for the real port.
    Split out from ``serve`` so a test can build + close a server without ever ``serve_forever``."""
    handler = _make_handler(
        root=root,
        engine_factory=engine_factory,
        usage_reader=usage_reader,
        clock=clock,
        snap_kwargs=snap_kwargs,
    )
    return ThreadingHTTPServer((host, port), handler)


def serve(
    root: Roots,
    engine_factory: EngineFactory,
    *,
    port: int,
    host: str = "127.0.0.1",
    usage_reader: Callable[[], object] | None = None,
    clock: Callable[[], float] = time.time,
    snap_kwargs: dict | None = None,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """Bind the server and serve it until interrupted. ``on_ready(url)`` fires with the bound
    URL just before the blocking loop (so the CLI can print the real port even under ``port=0``,
    and so nothing else is needed to know the address). Ctrl-C ends the loop cleanly.

    Everything up to ``serve_forever`` is exercised by ``build_server`` tests; only the blocking
    loop itself is the non-unit-testable line."""
    httpd = build_server(
        root,
        engine_factory,
        host=host,
        port=port,
        usage_reader=usage_reader,
        clock=clock,
        snap_kwargs=snap_kwargs,
    )
    raw_host = httpd.server_address[0]
    bound_host = raw_host.decode() if isinstance(raw_host, bytes) else raw_host
    bound_port = httpd.server_address[1]
    url = f"http://{bound_host}:{bound_port}/"
    if on_ready is not None:
        on_ready(url)
    try:
        httpd.serve_forever()  # pragma: no cover - the one blocking, non-unit-testable line
    except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl-C
        pass
    finally:
        httpd.server_close()


# --- self-contained static page ---------------------------------------------------------
# Inline HTML+CSS+JS, NO external asset (no CDN/font/image host) so it works fully offline and
# under a strict read-only-localhost posture; readable in light and dark via
# prefers-color-scheme. Laid out in the operator's order (#525): needs you (with each item's
# copyable release/resume command and a degraded row's real cause) → running now (per
# in-flight task: stage, time in stage, model, stream active/stalled, driver state in words,
# the live-stream drill-in over /api/stream) → progress per project (stage strip, waiting-on,
# issue/PR links, and a collapsible per-task panel with stage history, review findings and
# stage-file links over /api/task-detail + /api/stage-file) → cost (per run, stage and task,
# unmetered calls marked, usage headroom) → a recent-events timeline. It polls /api/snapshot
# and updates the DOM incrementally (keyed reconcile), so open panels, the active /
# attention / finished filter and the scroll position survive each refresh.

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>orchestrator dashboard</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f7f7f8; --fg: #1c1c1e; --muted: #6b6b70; --card: #ffffff; --line: #e2e2e6;
    --accent: #2563eb; --attn: #b91c1c; --attn-bg: #fef2f2; --warn: #a16207;
    --ok: #15803d; --run: #2563eb; --code-bg: #f0f0f3;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #16171a; --fg: #e6e6e9; --muted: #9a9aa2; --card: #1f2024; --line: #2c2d32;
      --accent: #60a5fa; --attn: #f87171; --attn-bg: #2a1414; --warn: #facc15;
      --ok: #4ade80; --run: #60a5fa; --code-bg: #26272c;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px; }
  a { color: var(--accent); }
  header { padding: 12px 18px; border-bottom: 1px solid var(--line); background: var(--card);
    position: sticky; top: 0; z-index: 2; }
  .top { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
  h1 { margin: 0; font-size: 16px; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted);
    margin: 0 0 8px; }
  h3 { font-size: 14px; margin: 0 0 6px; }
  .sub { color: var(--muted); font-size: 12px; }
  .filters { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; }
  .filters button { font: inherit; font-size: 12px; padding: 2px 10px; border-radius: 20px;
    border: 1px solid var(--line); background: var(--bg); color: var(--fg); cursor: pointer; }
  .filters button.on { border-color: var(--accent); color: var(--accent); font-weight: 600; }
  main { padding: 14px 18px; max-width: 1200px; margin: 0 auto; }
  section { margin-bottom: 26px; }
  .empty { color: var(--muted); font-size: 13px; }
  .empty.good { color: var(--ok); }
  .card { border: 1px solid var(--line); background: var(--card); border-radius: 8px;
    padding: 10px 12px; margin-bottom: 8px; }
  .attn { border-color: var(--attn); background: var(--attn-bg); }
  .attn .what { color: var(--attn); font-weight: 600; }
  .chip { font-size: 11px; color: var(--muted); border: 1px solid var(--line);
    border-radius: 4px; padding: 0 6px; margin-right: 6px; white-space: nowrap; }
  .err { color: var(--attn); }
  .warn { color: var(--warn); }
  .good { color: var(--ok); }
  .muted { color: var(--muted); }
  .cmd { display: flex; gap: 8px; align-items: center; margin-top: 6px; flex-wrap: wrap; }
  .cmd code { background: var(--code-bg); border: 1px solid var(--line); border-radius: 4px;
    padding: 2px 6px; overflow-x: auto; max-width: 100%; white-space: pre; }
  .cmd .label { font-size: 12px; color: var(--muted); }
  button.copy { font-size: 11px; padding: 1px 8px; border-radius: 4px; cursor: pointer;
    border: 1px solid var(--line); background: var(--bg); color: var(--fg); }
  .run-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
  .run-id { font-weight: 600; }
  .state { font-size: 11px; padding: 0 7px; border-radius: 20px; border: 1px solid var(--line);
    color: var(--muted); }
  .state.running { color: var(--run); border-color: var(--run); }
  .state.paused, .state.parked, .state.failed, .state.degraded { color: var(--attn);
    border-color: var(--attn); }
  .state.completed { color: var(--ok); border-color: var(--ok); }
  .rt { border-top: 1px solid var(--line); padding: 8px 0 4px; }
  .rt:first-of-type { border-top: 0; }
  .stage-name { font-weight: 600; }
  .drill { margin-top: 6px; }
  .tail { overflow-x: auto; background: var(--bg); border: 1px solid var(--line);
    border-radius: 6px; padding: 8px; white-space: pre; font-size: 12px; margin: 4px 0 0;
    max-height: 320px; }
  a.tabbtn { cursor: pointer; font-size: 12px; }
  details.task { border-top: 1px solid var(--line); padding: 6px 0; }
  details.task > summary { cursor: pointer; list-style-position: outside; }
  .strip { display: inline-flex; flex-wrap: wrap; gap: 3px; margin-left: 4px; }
  .st { font-size: 11px; padding: 0 5px; border-radius: 3px; border: 1px solid var(--line);
    color: var(--muted); }
  .st.completed { color: var(--ok); border-color: var(--ok); }
  .st.running { color: var(--run); border-color: var(--run); font-weight: 600; }
  .st.failed { color: var(--attn); border-color: var(--attn); }
  .st.skipped { text-decoration: line-through; }
  .task-body { padding: 6px 0 4px 18px; }
  table { border-collapse: collapse; font-size: 12px; margin: 4px 0 8px; }
  th, td { text-align: left; padding: 2px 10px 2px 0; border-bottom: 1px solid var(--line);
    vertical-align: top; }
  th { color: var(--muted); font-weight: 500; }
  td.num, th.num { text-align: right; }
  .finding { margin: 4px 0; }
  .finding .detail { color: var(--muted); font-size: 12px; white-space: pre-wrap; }
  .files a { margin-right: 10px; white-space: nowrap; }
  .legend { font-size: 12px; color: var(--muted); margin: 0 0 8px; }
  ol.timeline { list-style: none; padding: 0; margin: 0; }
  ol.timeline li { padding: 3px 0; border-bottom: 1px solid var(--line); font-size: 13px; }
  ol.timeline .when { color: var(--muted); font-size: 12px; margin-right: 8px; }
  .project-group { margin-bottom: 14px; }
  .cost-grid { display: flex; gap: 24px; flex-wrap: wrap; align-items: flex-start; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<header>
  <div class="top">
    <h1>orchestrator dashboard</h1>
    <span class="sub" id="clock"></span>
  </div>
  <div class="sub" id="summary">loading&hellip;</div>
  <nav class="filters" id="filters" aria-label="show runs">
    <button type="button" id="filter-all" data-filter="all">all</button>
    <button type="button" id="filter-active" data-filter="active">active</button>
    <button type="button" id="filter-attention" data-filter="attention">needs attention</button>
    <button type="button" id="filter-finished" data-filter="finished">finished</button>
  </nav>
</header>
<main>
  <section id="attention">
    <h2>needs you</h2>
    <div id="attention-list"></div>
    <div class="empty good" id="attention-empty" hidden>nothing needs you right now</div>
  </section>
  <section id="running">
    <h2>running now</h2>
    <div id="running-list"></div>
    <div class="empty" id="running-empty" hidden>nothing is running</div>
  </section>
  <section id="progress">
    <h2>progress</h2>
    <p class="legend">stage marks: &#10003; done &middot; &#9654; running &middot;
      &#10007; failed &middot; struck through = skipped &middot; plain = not started.
      Click a task for its stage history, review findings and log files.</p>
    <div id="progress-list"></div>
    <div class="empty" id="progress-empty" hidden>no runs match this filter</div>
  </section>
  <section id="cost">
    <h2>cost</h2>
    <div class="sub" id="usage"></div>
    <div class="sub" id="spend"></div>
    <div id="cost-list"></div>
  </section>
  <section id="recent">
    <h2>recent</h2>
    <ol class="timeline" id="recent-list"></ol>
    <div class="empty" id="recent-empty" hidden>no events yet</div>
  </section>
</main>
<script>
(function () {
  "use strict";
  var POLL_MS = 4000;
  var RECENT_LIMIT = 40;
  var FILTERS = ["all", "active", "attention", "finished"];
  var filter = (location.hash || "").slice(1);
  if (FILTERS.indexOf(filter) < 0) filter = "all";
  var streams = {};      // running-task key -> {stage, timer} for an open live-stream panel
  var details = {};      // task key -> last /api/task-detail payload
  var lastSnap = null;

  // --- small helpers ---------------------------------------------------------------------

  function h(tag, cls, text) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }

  function link(href, text) {
    var a = h("a", null, text);
    a.href = href; a.target = "_blank"; a.rel = "noopener";
    return a;
  }

  function fill(el) {
    el.textContent = "";
    for (var i = 1; i < arguments.length; i++) {
      var c = arguments[i];
      if (c == null) continue;
      el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return el;
  }

  // The child of `el` with class `cls`, created on first use. Parts persist across rebuilds,
  // so a live-stream panel or a fetched file list is not torn down by an unrelated update.
  function part(el, cls, tag) {
    for (var i = 0; i < el.children.length; i++)
      if (el.children[i].classList.contains(cls)) return el.children[i];
    var c = h(tag || "div", cls);
    el.appendChild(c);
    return c;
  }

  // Keyed, incremental list update: an item whose data is unchanged keeps its DOM node
  // untouched; a changed item is rebuilt IN PLACE (so a <details> keeps its open state);
  // new items are inserted, vanished ones removed. This is what lets open panels, the
  // filter and the scroll position survive each poll.
  function reconcile(box, items, keyOf, build, tag) {
    var old = {};
    Array.prototype.slice.call(box.children).forEach(function (el) {
      if (el.dataset.key != null) old[el.dataset.key] = el;
    });
    var prev = null;
    items.forEach(function (item) {
      var key = keyOf(item);
      var sig = JSON.stringify(item);
      var el = old[key];
      if (el) delete old[key];
      else { el = document.createElement(tag || "div"); el.dataset.key = key; }
      if (el.dataset.sig !== sig) { build(el, item); el.dataset.sig = sig; }
      var want = prev ? prev.nextSibling : box.firstChild;
      if (el !== want) box.insertBefore(el, want);
      prev = el;
    });
    Object.keys(old).forEach(function (k) { box.removeChild(old[k]); });
  }

  function fmtAge(s) {
    if (s == null) return "?";
    if (s < 90) return Math.floor(s) + "s";
    if (s < 5400) return Math.floor(s / 60) + "m";
    return (s / 3600).toFixed(1) + "h";
  }

  function fmtTime(ts) {
    if (!ts) return "";
    var d = new Date(ts);
    return isNaN(d.getTime()) ? String(ts) : d.toLocaleTimeString();
  }

  function fmtTokens(n) {
    if (!n) return "0";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  }

  function words(s) { return String(s == null ? "" : s).split("_").join(" "); }

  // Aggregate cost cell — the JS twin of render.aggregate_cost_cell (#319/#331). Unmetered
  // calls sum into the total at $0, so a partly-unmetered bucket is a FLOOR ("≥$X") and a
  // wholly-unmetered one is unknown ("n/a (unmetered)") — never a bare, confident figure.
  function costCell(cost, unmetered, invocations) {
    if (typeof cost !== "number") return "$?";
    unmetered = unmetered || 0;
    invocations = invocations || 0;
    if (unmetered && invocations && unmetered >= invocations) return "n/a (unmetered)";
    if (unmetered) return "≥$" + cost.toFixed(4);
    return "$" + cost.toFixed(4);
  }

  function bucketCost(b) {
    if (!b) return "—";
    return costCell(b.cost_usd, b.unmetered_calls, b.calls);
  }

  function unmeteredNote(n) {
    return n ? h("span", "warn", " (" + n + " unmetered, cost unknown)") : null;
  }

  var TASK_STATE = {
    pending: "not started", running: "running", retrying: "retrying",
    blocked: "blocked", cascade_blocked: "blocked by a failed dependency",
    blocked_on_human: "waiting for your decision", completed: "done", failed: "failed",
    closed_infeasible: "closed as infeasible", superseded: "superseded"
  };
  function taskState(s) { return TASK_STATE[s] || words(s); }

  var STAGE_MARK = { completed: " ✓", running: " ▶", failed: " ✗" };

  function rowKey(row) { return (row.root || "") + "|" + row.run_id; }

  function rowMatches(row, f) {
    if (f === "active") return !row.terminal;
    if (f === "attention") return !!row.attention;
    if (f === "finished") return !!row.terminal;
    return true;
  }

  function rowState(row) { return row.degraded ? "degraded" : (row.state || "?"); }

  function taskTitle(t) {
    var span = h("span");
    var id = t.issue_url ? link(t.issue_url, t.task_id) : h("span", "mono", t.task_id);
    fill(span, id, t.title ? " " + t.title : null);
    return span;
  }

  function query(row, taskId, extra) {
    var q = "run=" + encodeURIComponent(row.run_id) + "&task=" + encodeURIComponent(taskId);
    // #386: the board spans runs-roots, so send the row's own root — the server refuses to
    // guess when two roots hold the same run id.
    if (row.root) q += "&root=" + encodeURIComponent(row.root);
    return q + (extra || "");
  }

  // --- needs you ---------------------------------------------------------------------------

  var KIND = {
    blocked_on_human: "blocked, waiting for your decision",
    paused: "run paused",
    parked: "run parked, needs a fresh supervisor session",
    budget_exhausted: "budget used up",
    stale: "no progress",
    unreadable: "run status could not be read",
    adapter_unresolved: "project adapter could not be loaded"
  };

  function attnDetail(it) {
    if (it.kind === "stale")
      return "no update for " + fmtAge(it.seconds_since_update)
        + (it.stage ? " at " + String(it.stage).toUpperCase() : "")
        + " — check its live stream or stage logs below";
    if (it.kind === "budget_exhausted") {
      var f = it.fraction;
      return "spend is at " + (typeof f === "number" ? Math.round(f * 100) + "%" : "?")
        + " of the run's budget";
    }
    if (it.kind === "unreadable" && !it.error)
      return (it.reason || "") + " — inspect runs/" + it.run_id + "/ by hand";
    return it.reason || "";
  }

  function buildAttention(el, it) {
    el.className = "card attn";
    var head = fill(h("div"),
      h("span", "chip", it.project || "?"), h("span", "run-id", it.run_id), "  ",
      it.task_id ? h("span", "mono", it.task_id + " ") : null,
      it.title ? h("span", null, it.title + " ") : null,
      h("span", "what", "— " + (KIND[it.kind] || words(it.kind))));
    var nodes = [head];
    var detail = attnDetail(it);
    if (detail) nodes.push(h("div", null, detail));
    // #498: a degraded row names the exception that caused it, not just "unreadable".
    if (it.error)
      nodes.push(h("div", "err mono", "cause: " + it.error.type
        + (it.error.message ? ": " + it.error.message : "")));
    (it.commands || []).forEach(function (c) {
      var row = h("div", "cmd");
      fill(row, h("span", "label", c.label + ":"), h("code", null, c.command),
        h("button", "copy", "copy"));
      nodes.push(row);
    });
    fill.apply(null, [el].concat(nodes));
  }

  function renderAttention(snap, visible, titles) {
    var items = (snap.attention || []).filter(function (it) { return visible[it.run_id]; })
      .map(function (it) {
        var extra = visible[it.run_id];
        return Object.assign({}, it, {
          project: extra.project,
          title: it.task_id ? titles[it.run_id + "|" + it.task_id] || "" : ""
        });
      });
    reconcile(document.getElementById("attention-list"), items, function (it) {
      return it.run_id + "|" + it.kind + "|" + (it.task_id || "");
    }, buildAttention);
    document.getElementById("attention-empty").hidden = items.length > 0;
  }

  // --- running now -------------------------------------------------------------------------

  function stopStream(key) {
    var s = streams[key];
    if (s) { clearInterval(s.timer); delete streams[key]; }
  }

  function startStream(key, item, drill) {
    stopStream(key);
    var q = query(item.row, item.task.task_id,
      item.inf.stage ? "&stage=" + encodeURIComponent(item.inf.stage) : "");
    var tailEl = h("pre", "tail", "loading…");
    fill(drill, h("div", "muted", "live stream — " + item.task.task_id
      + (item.inf.stage ? " · " + item.inf.stage : "")), tailEl);
    drill.hidden = false;
    function poll() {
      fetch("/api/stream?" + q).then(function (r) {
        if (r.status === 404) { tailEl.textContent = "(no live provider stream)"; return null; }
        return r.json();
      }).then(function (p) {
        if (!p) return;
        var act = p.current_activity
          ? (p.current_activity.tool + (p.current_activity.detail ? ": "
            + p.current_activity.detail : "")) : "working";
        tailEl.textContent = p.events_seen + " events · " + act + String.fromCharCode(10, 10)
          + (p.recent_tail || []).join(String.fromCharCode(10));
      }).catch(function () {});
    }
    poll();
    streams[key] = { stage: item.inf.stage, timer: setInterval(poll, POLL_MS) };
  }

  function activityLine(t) {
    var a = t.activity || {};
    if (a.state === "active")
      return h("span", "good", "stream active (last output " + fmtAge(a.seconds_since_event)
        + " ago)" + (a.line ? ": " + a.line : ""));
    if (a.state === "stalled")
      return h("span", "warn", "stream stalled, no output for "
        + fmtAge(a.seconds_since_event) + (a.line ? " (last: " + a.line + ")" : ""));
    return h("span", "muted", "no live stream");
  }

  function buildRunningTask(el, item) {
    el.className = "rt";
    var t = item.task;
    var key = el.dataset.key;
    fill(part(el, "rt-info"),
      taskTitle(t), "  ",
      h("span", "stage-name", String(t.current_stage || "?").toUpperCase()),
      " for " + fmtAge(t.stage_age_s),
      t.model ? " on " + t.model : null);
    fill(part(el, "rt-act"), activityLine(t));
    var ctl = part(el, "rt-ctl");
    var drill = part(el, "drill");
    var inf = item.inf;
    // Only offer the stream affordance when a tailable provider stream actually exists
    // (#137). On the interactive×claude / ENGINE lanes stages run in-session with nothing
    // teeing provider stdout to disk, so the toggle would only ever open an empty panel —
    // show an honest lane note instead of advertising a stream that can't populate.
    if (inf && inf.stream_available) {
      var btn = h("a", "tabbtn", streams[key] ? "hide live stream" : "show live stream");
      btn.addEventListener("click", function () {
        if (streams[key]) {
          stopStream(key); drill.hidden = true; btn.textContent = "show live stream";
        } else {
          startStream(key, item, drill); btn.textContent = "hide live stream";
        }
      });
      fill(ctl, btn);
      if (streams[key] && streams[key].stage !== inf.stage) startStream(key, item, drill);
    } else {
      fill(ctl, h("span", "muted",
        "in-session lane — no tailable stream; follow events.jsonl / per-stage logs"));
      stopStream(key);
      drill.hidden = true;
    }
  }

  function buildRunningRun(el, item) {
    var row = item.row;
    el.className = "card";
    var d = row.driver || {};
    var driverCls = d.state === "dead" ? "err"
      : (d.state === "capacity_wait" || d.state === "cooldown_wait") ? "warn" : "muted";
    fill(part(el, "run-head"),
      h("span", "chip", row.project || "?"), h("span", "run-id", row.run_id),
      h("span", "state " + rowState(row), words(rowState(row))),
      h("span", driverCls, "driver: " + (d.summary || "unknown")));
    var list = part(el, "rt-list");
    var running = item.tasks.map(function (t) {
      var inf = (row.inflight || []).filter(function (i) { return i.task_id === t.task_id; })[0];
      return { row: { run_id: row.run_id, root: row.root }, task: t, inf: inf || null };
    });
    reconcile(list, running, function (r) { return rowKey(row) + "|" + r.task.task_id; },
      buildRunningTask);
    var waiting = (row.tasks || []).filter(function (t) {
      return t.state === "pending" || t.state === "retrying";
    }).length;
    fill(part(el, "rt-foot"), running.length ? null
      : h("div", "muted", "nothing running right now"
        + (waiting ? "; " + waiting + " task(s) not started yet" : "")));
  }

  function renderRunning(rows) {
    var items = rows.filter(function (r) { return !r.terminal && !r.degraded; })
      .map(function (row) {
        return {
          row: row,
          tasks: (row.tasks || []).filter(function (t) { return t.state === "running"; })
        };
      });
    reconcile(document.getElementById("running-list"), items,
      function (it) { return rowKey(it.row); }, buildRunningRun);
    var live = {};
    items.forEach(function (it) {
      it.tasks.forEach(function (t) { live[rowKey(it.row) + "|" + t.task_id] = true; });
    });
    Object.keys(streams).forEach(function (k) { if (!live[k]) stopStream(k); });
    document.getElementById("running-empty").hidden = items.length > 0;
  }

  // --- progress ----------------------------------------------------------------------------

  function stageStrip(stages) {
    var strip = h("span", "strip");
    (stages || []).forEach(function (s) {
      var chip = h("span", "st " + s.status, s.stage + (STAGE_MARK[s.status] || ""));
      chip.title = words(s.status) + (s.attempt ? ", attempt " + (s.attempt + 1) : "")
        + (s.model ? ", " + s.model : "");
      strip.appendChild(chip);
    });
    return strip;
  }

  function findingNode(f) {
    var box = h("div", "finding");
    if (typeof f !== "object" || f == null) return fill(box, String(f));
    var tag = f.severity || f.disposition;
    fill(box, h("span", null, f.title || f.summary || JSON.stringify(f)),
      tag ? h("span", "chip", words(tag)) : null,
      f.file ? h("span", "muted mono", " " + f.file + (f.line ? ":" + f.line : "")) : null);
    if (f.detail || f.description) box.appendChild(h("div", "detail", f.detail || f.description));
    return box;
  }

  function fileKind(name) {
    if (/[.]prompt[.]txt$/.test(name)) return "prompts";
    if (/[.](stream[.]jsonl|stderr[.]log)$/.test(name)) return "provider logs";
    if (/^[0-9]+-/.test(name)) return "stage records";
    return "notes";
  }

  function renderTaskExtras(box, item, data) {
    if (!data) { fill(box, h("div", "muted", "loading files…")); return; }
    if (data.error) { fill(box, h("div", "muted", data.error)); return; }
    var nodes = [];
    var rv = data.review;
    if (rv) {
      nodes.push(h("h3", null, "review (" + rv.file + ")"));
      if (rv.error) nodes.push(h("div", "err", rv.error));
      else {
        nodes.push(h("div", rv.approved ? "good" : "err",
          rv.approved ? "approved" : "changes requested"));
        if ((rv.issues || []).length) {
          nodes.push(h("div", "err", "blocking findings"));
          rv.issues.forEach(function (f) { nodes.push(findingNode(f)); });
        }
        if ((rv.non_blocking || []).length) {
          nodes.push(h("div", "muted", "non-blocking findings"));
          rv.non_blocking.forEach(function (f) { nodes.push(findingNode(f)); });
        }
      }
    }
    var groups = {};
    (data.files || []).forEach(function (f) {
      (groups[fileKind(f.name)] = groups[fileKind(f.name)] || []).push(f);
    });
    ["prompts", "provider logs", "stage records", "notes"].forEach(function (g) {
      if (!groups[g]) return;
      var line = h("div", "files");
      line.appendChild(h("span", "muted", g + ": "));
      groups[g].forEach(function (f) {
        var a = link("/api/stage-file?" + query(item.row, item.task.task_id,
          "&name=" + encodeURIComponent(f.name)), f.name);
        a.title = f.size + " bytes";
        line.appendChild(a);
      });
      nodes.push(line);
    });
    if (!nodes.length) nodes.push(h("div", "muted", "no stage files yet"));
    fill.apply(null, [box].concat(nodes));
  }

  function loadTaskExtras(el, item) {
    var key = el.dataset.key;
    var box = part(el, "task-extras");
    renderTaskExtras(box, item, details[key]);
    fetch("/api/task-detail?" + query(item.row, item.task.task_id)).then(function (r) {
      if (r.status === 404) return { error: "no stage files for this task yet" };
      return r.json();
    }).then(function (data) {
      details[key] = data;
      if (el.open) renderTaskExtras(part(el, "task-extras"), item, data);
    }).catch(function () {});
  }

  function stageHistory(t) {
    var tbl = h("table");
    var head = h("tr");
    ["stage", "status", "attempts", "model", "time", "cost"].forEach(function (c) {
      head.appendChild(h("th", c === "cost" || c === "time" ? "num" : null, c));
    });
    tbl.appendChild(head);
    (t.stages || []).forEach(function (s) {
      var tr = h("tr");
      var cost = s.calls ? costCell(s.cost_usd, s.unmetered_calls, s.calls)
        : (typeof s.cost_usd === "number" && s.cost_usd
          ? (s.metered === false ? "n/a (unmetered)" : "$" + s.cost_usd.toFixed(2)) : "—");
      [s.stage, words(s.status), s.status === "pending" ? "" : String((s.attempt || 0) + 1),
        s.model || "", s.duration_s == null ? "" : fmtAge(s.duration_s), cost
      ].forEach(function (v, i) {
        tr.appendChild(h("td", i >= 4 ? "num" : null, v));
      });
      tbl.appendChild(tr);
    });
    return tbl;
  }

  function buildTask(el, item) {
    var t = item.task;
    el.className = "task";
    if (!el.dataset.wired) {
      el.dataset.wired = "1";
      el.addEventListener("toggle", function () {
        if (el.open) loadTaskExtras(el, JSON.parse(el.dataset.item));
      });
    }
    el.dataset.item = JSON.stringify(item);
    var stCls = t.state === "failed" || t.state === "blocked_on_human" ? "err"
      : t.state === "completed" ? "good" : "muted";
    fill(part(el, "task-sum", "summary"),
      taskTitle(t), "  ", h("span", stCls, taskState(t.state)), stageStrip(t.stages),
      (t.waiting_on || []).length ? h("span", "warn", "  waiting on " + t.waiting_on.join(", "))
        : null,
      t.pr_url ? h("span", null, "  ") : null, t.pr_url ? link(t.pr_url, "PR") : null);
    var body = part(el, "task-body");
    fill(part(body, "task-history"),
      t.blocked_reason ? h("div", "err", "blocked: " + t.blocked_reason) : null,
      (t.depends_on || []).length ? h("div", "muted", "depends on " + t.depends_on.join(", "))
        : null,
      stageHistory(t));
    if (el.open) loadTaskExtras(el, item);
  }

  function buildProgressRun(el, row) {
    el.className = "card";
    var prog = row.progress || {};
    var done = (prog.completed || 0) + (prog.closed_infeasible || 0) + (prog.superseded || 0);
    var head = fill(part(el, "run-head"),
      h("span", "run-id", row.run_id),
      h("span", "state " + rowState(row), words(rowState(row))),
      row.degraded ? null : h("span", "muted", done + " of " + (prog.total || 0) + " tasks done"),
      h("span", "muted", "last event " + fmtAge(row.last_event_age_s) + " ago"));
    if (row.degraded && row.error)
      head.appendChild(h("span", "err mono", row.error.type
        + (row.error.message ? ": " + row.error.message : "")));
    var items = (row.tasks || []).map(function (t) {
      return { row: { run_id: row.run_id, root: row.root }, task: t };
    });
    reconcile(part(el, "task-list"), items, function (it) {
      return rowKey(row) + "|" + it.task.task_id;
    }, buildTask, "details");
  }

  function buildProject(el, group) {
    el.className = "project-group";
    fill(part(el, "project-head", "h3"), group.project);
    reconcile(part(el, "project-runs"), group.rows, rowKey, buildProgressRun);
  }

  function renderProgress(rows) {
    var groups = [];
    var byName = {};
    rows.forEach(function (row) {
      var name = row.project || "?";
      if (!byName[name]) { byName[name] = { project: name, rows: [] }; groups.push(byName[name]); }
      byName[name].rows.push(row);
    });
    reconcile(document.getElementById("progress-list"), groups,
      function (g) { return g.project; }, buildProject);
    document.getElementById("progress-empty").hidden = groups.length > 0;
  }

  // --- cost --------------------------------------------------------------------------------

  function costTable(label, buckets, names) {
    var keys = Object.keys(buckets || {});
    if (!keys.length) return null;
    var tbl = h("table");
    var head = h("tr");
    [label, "cost", "calls", "tokens in", "tokens out"].forEach(function (c, i) {
      head.appendChild(h("th", i ? "num" : null, c));
    });
    tbl.appendChild(head);
    keys.forEach(function (k) {
      var b = buckets[k];
      var tr = h("tr");
      tr.appendChild(h("td", null, k + (names && names[k] ? " " + names[k] : "")));
      var cost = h("td", "num", bucketCost(b));
      if (b.unmetered_calls) {
        cost.className = "num warn";
        cost.title = b.unmetered_calls + " unmetered call(s) of unknown cost (not $0)";
      }
      tr.appendChild(cost);
      tr.appendChild(h("td", "num", b.calls + (b.unmetered_calls
        ? " (" + b.unmetered_calls + " unmetered)" : "")));
      tr.appendChild(h("td", "num", fmtTokens(b.input_tokens)));
      tr.appendChild(h("td", "num", fmtTokens(b.output_tokens)));
      tbl.appendChild(tr);
    });
    return tbl;
  }

  function buildCostRun(el, row) {
    el.className = "card";
    var b = row.cost_breakdown || {};
    var names = {};
    (row.tasks || []).forEach(function (t) { if (t.title) names[t.task_id] = t.title; });
    fill(part(el, "run-head"),
      h("span", "chip", row.project || "?"), h("span", "run-id", row.run_id),
      h("span", null, costCell(row.cost_usd, row.unmetered_calls, row.total_invocations)),
      unmeteredNote(row.unmetered_calls),
      h("span", "muted", (row.total_invocations || 0) + " call(s) · tokens in "
        + fmtTokens(b.input_tokens) + " / out " + fmtTokens(b.output_tokens)),
      row.legacy_accounting_rows ? h("span", "warn",
        "priced under the old accounting (overstated)") : null);
    var budget = row.budget;
    var grid = fill(part(el, "cost-grid"),
      costTable("stage", b.by_stage), costTable("task", b.by_task, names));
    if (budget && typeof budget.fraction === "number")
      grid.insertBefore(h("div", budget.exhausted ? "err" : "muted",
        "budget: " + Math.round(budget.fraction * 100) + "% used"), grid.firstChild);
  }

  function usageLine(u) {
    if (!u || u.five_hour_pct == null) return "account usage: unavailable";
    function win(label, pct) {
      if (pct == null) return label + " unknown";
      var used = Math.round(pct);
      return label + " " + used + "% used, " + Math.max(0, 100 - used) + "% left";
    }
    var resets = u.five_hour_resets_at ? " (resets " + fmtTime(u.five_hour_resets_at) + ")" : "";
    // #386: the probe reads the ACCOUNT's window, shared by every project on this board.
    return "account usage headroom: " + win("5-hour window", u.five_hour_pct) + resets
      + " · " + win("7-day window", u.seven_day_pct);
  }

  function spendLine(hd) {
    // #331: unmetered calls add $0 to total_spend_usd, so an unqualified total understates.
    var unmetered = hd.unmetered_calls || 0;
    var calls = hd.total_invocations || 0;
    var s;
    if (unmetered && calls && unmetered >= calls)
      s = "spend unknown: all " + unmetered + " call(s) unmetered";
    else if (unmetered)
      s = "spend at least $" + (hd.total_spend_usd || 0).toFixed(2) + " (" + unmetered
        + " unmetered call(s) of unknown cost not included)";
    else s = "spend $" + (hd.total_spend_usd || 0).toFixed(2);
    s += " across " + (hd.shown || 0) + " run(s) shown";
    if (hd.legacy_accounting_runs)
      s += " · " + hd.legacy_accounting_runs
        + " run(s) priced under the old accounting, so this total mixes two pricing regimes";
    return s;
  }

  function renderCost(snap, rows) {
    var hd = snap.header || {};
    document.getElementById("usage").textContent = usageLine(hd.usage);
    document.getElementById("spend").textContent = spendLine(hd);
    reconcile(document.getElementById("cost-list"),
      rows.filter(function (r) { return !r.degraded; }), rowKey, buildCostRun);
  }

  // --- recent ------------------------------------------------------------------------------

  function renderRecent(rows) {
    var evs = [];
    rows.forEach(function (row) {
      (row.recent_events || []).forEach(function (e) {
        evs.push(Object.assign({ run_id: row.run_id }, e));
      });
    });
    evs.sort(function (a, b) { return String(b.ts || "").localeCompare(String(a.ts || "")); });
    evs = evs.slice(0, RECENT_LIMIT);
    reconcile(document.getElementById("recent-list"), evs, function (e) {
      return [e.run_id, e.ts, e.type, e.task_id, e.sentence].join("|");
    }, function (el, e) {
      el.className = e.level === "warning" || e.level === "error" ? "warn" : "";
      fill(el, h("span", "when", fmtTime(e.ts)), h("span", "chip", e.run_id), e.sentence);
    }, "li");
    document.getElementById("recent-empty").hidden = evs.length > 0;
  }

  // --- header, filters, poll ---------------------------------------------------------------

  function renderHeader(snap) {
    var hd = snap.header || {};
    var projects = (hd.projects || []).map(function (p) {
      return p.project + " (" + p.runs + " run" + (p.runs === 1 ? "" : "s")
        + (p.running ? ", " + p.running + " running" : "")
        + (p.attention ? ", " + p.attention + " need you" : "") + ")";
    });
    document.getElementById("summary").textContent =
      (hd.all_quiet ? "nothing needs you" : hd.attention_count + " item(s) need you")
      + " · " + (hd.running || 0) + " run(s) running"
      + (projects.length ? " · " + projects.join(", ") : "");
    document.getElementById("clock").textContent =
      hd.generated_at ? "updated " + fmtTime(hd.generated_at) : "";
    var runs = snap.runs || [];
    FILTERS.forEach(function (f) {
      var btn = document.getElementById("filter-" + f);
      var n = runs.filter(function (r) { return rowMatches(r, f); }).length;
      btn.textContent = btn.textContent.split(" (")[0] + " (" + n + ")";
      btn.className = f === filter ? "on" : "";
      btn.setAttribute("aria-pressed", f === filter ? "true" : "false");
    });
  }

  function render(snap) {
    lastSnap = snap;
    var rows = (snap.runs || []).filter(function (r) { return rowMatches(r, filter); });
    var visible = {};
    var titles = {};
    rows.forEach(function (r) {
      visible[r.run_id] = r;
      (r.tasks || []).forEach(function (t) { titles[r.run_id + "|" + t.task_id] = t.title; });
    });
    renderHeader(snap);
    renderAttention(snap, visible, titles);
    renderRunning(rows);
    renderProgress(rows);
    renderCost(snap, rows);
    renderRecent(rows);
  }

  document.getElementById("filters").addEventListener("click", function (e) {
    var f = e.target && e.target.getAttribute("data-filter");
    if (!f) return;
    filter = f;
    history.replaceState(null, "", "#" + f);
    if (lastSnap) render(lastSnap);
  });

  document.addEventListener("click", function (e) {
    var btn = e.target;
    if (!btn.classList || !btn.classList.contains("copy")) return;
    var code = btn.parentNode.querySelector("code");
    var text = code ? code.textContent : "";
    function done(msg) {
      btn.textContent = msg;
      setTimeout(function () { btn.textContent = "copy"; }, 1500);
    }
    function fallback() {
      var range = document.createRange();
      range.selectNodeContents(code);
      var sel = window.getSelection();
      sel.removeAllRanges(); sel.addRange(range);
      done("selected, press copy");
    }
    if (navigator.clipboard && navigator.clipboard.writeText)
      navigator.clipboard.writeText(text).then(function () { done("copied"); }, fallback);
    else fallback();
  });

  function poll() {
    fetch("/api/snapshot").then(function (r) { return r.json(); }).then(render)
      .catch(function (e) {
        document.getElementById("summary").textContent = "could not refresh: " + e;
      });
  }

  poll();
  setInterval(poll, POLL_MS);
})();
</script>
</body>
</html>
"""
