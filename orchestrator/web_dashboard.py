"""Read-only web skin over the cross-session board (#94).

The terminal ``orchestrator dashboard`` (#6) already assembles the whole picture: a pure
``dashboard_snapshot(root, ...)`` folds every ``runs/<id>/`` store into an attention-first
board, and ``stream_probe.probe_current_stream`` turns a partially-written stage stream into a
live activity snapshot. This module is the thin HTTP layer that serves those two data functions
as JSON plus a self-contained static page that polls them — no engine changes, read-only, and
still project-agnostic (the engine is never touched for model work; the board only *reads*).

The routing is factored into a PURE ``route_request`` so the whole surface is unit-testable
without a socket: it serves the inlined static page for ``/``, ``dashboard_snapshot`` as JSON for
``/api/snapshot``, and ``probe_current_stream`` (over ``root/<run>``) as JSON for ``/api/stream``.
The server wrapper (``build_server`` / ``serve``) is a tiny GET-only handler around it; only the
final ``serve_forever`` loop is not exercised by a unit test.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from .dashboard import dashboard_snapshot
from .stream_probe import probe_current_stream

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .engine import Engine

_JSON_CT = "application/json; charset=utf-8"
_HTML_CT = "text/html; charset=utf-8"


def _json(status: int, obj: object) -> tuple[int, str, bytes]:
    """A (status, content-type, body) JSON triple. ``default=str`` is a belt-and-suspenders
    guard so an unexpected non-serializable value degrades to its ``str`` instead of raising."""
    return status, _JSON_CT, json.dumps(obj, default=str).encode("utf-8")


def _first(query: dict[str, list[str]], key: str) -> str | None:
    """The first value for a parsed query-string key, or ``None`` (missing/empty)."""
    vals = query.get(key)
    return vals[0] if vals else None


def route_request(
    path: str,
    query: dict[str, list[str]],
    *,
    root: str | Path,
    engine_factory: Callable[[Path], Engine],
    usage_reader: Callable[[], object] | None = None,
    clock: Callable[[], float] = time.time,
    snap_kwargs: dict | None = None,
) -> tuple[int, str, bytes]:
    """Map one GET (path + parsed query) to a ``(status, content_type, body)`` triple. Pure:
    no socket, no globals — the server handler and the tests both drive exactly this.

    Routes:
      - ``/`` (and ``/index.html``) → the inlined, self-contained static page.
      - ``/api/snapshot`` → ``dashboard_snapshot(root, ...)`` as JSON.
      - ``/api/stream?run=&task=&stage=`` → ``probe_current_stream(root/<run>, task, stage)`` as
        JSON, or ``404`` when there is no stream to probe (interactive/ENGINE lane, or nothing
        dispatched yet). Missing ``run``/``task`` → ``400``.
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
        probe = probe_current_stream(Path(root) / run, task, stage)
        if probe is None:
            return _json(404, {"error": "no stream", "run": run, "task": task, "stage": stage})
        return _json(200, {"run": run, "task": task, "stage": stage, **probe})
    return _json(404, {"error": "not found", "path": path})


# --- server wrapper ---------------------------------------------------------------------


def _make_handler(
    *,
    root: str | Path,
    engine_factory: Callable[[Path], Engine],
    usage_reader: Callable[[], object] | None,
    clock: Callable[[], float],
    snap_kwargs: dict | None,
) -> type[BaseHTTPRequestHandler]:
    """A GET-only ``BaseHTTPRequestHandler`` subclass that delegates every request to the pure
    ``route_request``. The read config is captured in the closure so nothing global is needed."""

    class _Handler(BaseHTTPRequestHandler):
        server_version = "orchestrator-dashboard/1"

        def log_message(self, *_args) -> None:  # noqa: D401 - quiet the default stderr spam
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
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler


def build_server(
    root: str | Path,
    engine_factory: Callable[[Path], Engine],
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
    root: str | Path,
    engine_factory: Callable[[Path], Engine],
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
# under a strict read-only-localhost posture. Polls /api/snapshot on an interval, renders the
# attention band first, then the per-run rows, with a per-run drill-in that polls /api/stream.

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>orchestrator dashboard</title>
<style>
  :root {
    --bg: #f7f7f8; --fg: #1c1c1e; --muted: #6b6b70; --card: #ffffff; --line: #e2e2e6;
    --accent: #2563eb; --attn: #b91c1c; --attn-bg: #fef2f2; --ok: #15803d; --run: #2563eb;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #16171a; --fg: #e6e6e9; --muted: #9a9aa2; --card: #1f2024; --line: #2c2d32;
      --accent: #60a5fa; --attn: #f87171; --attn-bg: #2a1414; --ok: #4ade80; --run: #60a5fa;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  header { padding: 14px 18px; border-bottom: 1px solid var(--line); }
  h1 { margin: 0 0 4px; font-size: 15px; letter-spacing: .02em; }
  .sub { color: var(--muted); font-size: 12px; }
  main { padding: 14px 18px; max-width: 1100px; margin: 0 auto; }
  section { margin-bottom: 20px; }
  .band-title { font-size: 12px; text-transform: uppercase; letter-spacing: .08em;
    color: var(--muted); margin: 0 0 8px; }
  .attn { border: 1px solid var(--line); background: var(--attn-bg); border-radius: 8px;
    padding: 8px 12px; margin-bottom: 6px; color: var(--attn); }
  .quiet { color: var(--ok); }
  .run { border: 1px solid var(--line); background: var(--card); border-radius: 8px;
    padding: 10px 12px; margin-bottom: 8px; }
  .run.needs { border-color: var(--attn); }
  .run-head { display: flex; flex-wrap: wrap; gap: 10px; align-items: baseline;
    cursor: pointer; }
  .run-id { font-weight: 600; }
  .badge { font-size: 11px; padding: 1px 7px; border-radius: 20px; border: 1px solid var(--line);
    color: var(--muted); }
  .badge.running { color: var(--run); border-color: var(--run); }
  .badge.paused, .badge.failed { color: var(--attn); border-color: var(--attn); }
  .badge.completed { color: var(--ok); border-color: var(--ok); }
  .grow { flex: 1; }
  .muted { color: var(--muted); }
  .flags { color: var(--attn); font-size: 12px; }
  .inflight { margin-top: 6px; color: var(--muted); font-size: 12px; }
  .drill { margin-top: 8px; border-top: 1px dashed var(--line); padding-top: 8px; }
  .tail { overflow-x: auto; background: var(--bg); border: 1px solid var(--line);
    border-radius: 6px; padding: 8px; white-space: pre; font-size: 12px; }
  .err { color: var(--attn); }
  table { border-collapse: collapse; }
  a.tabbtn { color: var(--accent); cursor: pointer; text-decoration: underline; }
</style>
</head>
<body>
<header>
  <h1>orchestrator dashboard <span class="sub" id="clock"></span></h1>
  <div class="sub" id="summary">loading&hellip;</div>
</header>
<main>
  <section id="attention"></section>
  <section id="runs"></section>
</main>
<script>
(function () {
  "use strict";
  var POLL_MS = 4000;
  var open = {};       // run_id -> {task, stage} currently drilled in
  var streamTimers = {};

  function h(tag, cls, text) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }

  function fmtAge(s) {
    if (s == null) return "?";
    if (s < 90) return Math.floor(s) + "s";
    if (s < 5400) return Math.floor(s / 60) + "m";
    return Math.floor(s / 3600) + "h";
  }

  function attnLine(it) {
    var run = it.run_id;
    if (it.kind === "blocked_on_human")
      return "! [" + run + "] " + it.task_id + " BLOCKED_ON_HUMAN — " + (it.reason || "");
    if (it.kind === "paused")
      return "! [" + run + "] PAUSED — " + (it.reason || "");
    if (it.kind === "budget_exhausted") {
      var f = it.fraction;
      var pct = (typeof f === "number") ? Math.round(f * 100) + "%" : "?";
      return "! [" + run + "] BUDGET EXHAUSTED — spend at " + pct + " of budget";
    }
    if (it.kind === "stale")
      return "! [" + run + "] " + it.task_id + " STALE — no update for "
        + fmtAge(it.seconds_since_update) + " (stage " + it.stage + ")";
    if (it.kind === "unreadable")
      return "! [" + run + "] UNREADABLE status — inspect runs/" + run + "/ by hand";
    return "! [" + run + "] " + it.kind;
  }

  function renderAttention(snap) {
    var box = document.getElementById("attention");
    box.innerHTML = "";
    var items = snap.attention || [];
    box.appendChild(h("p", "band-title", items.length ? "needs you" : "attention"));
    if (!items.length) {
      box.appendChild(h("div", "quiet", "✓ all quiet — nothing needs a human"));
      return;
    }
    items.forEach(function (it) { box.appendChild(h("div", "attn", attnLine(it))); });
  }

  function drillStream(runId, inf, container) {
    var q = "run=" + encodeURIComponent(runId) + "&task=" + encodeURIComponent(inf.task_id);
    if (inf.stage) q += "&stage=" + encodeURIComponent(inf.stage);
    container.innerHTML = "";
    container.appendChild(h("div", "muted", "live stream — " + inf.task_id
      + (inf.stage ? " · " + inf.stage : "")));
    var tailEl = h("div", "tail", "loading…");
    container.appendChild(tailEl);
    function poll() {
      fetch("/api/stream?" + q).then(function (r) {
        if (r.status === 404) { tailEl.className = "tail muted";
          tailEl.textContent = "(no live provider stream)"; return null; }
        return r.json();
      }).then(function (p) {
        if (!p) return;
        var act = p.current_activity
          ? (p.current_activity.tool + (p.current_activity.detail ? ": "
            + p.current_activity.detail : "")) : "working";
        var head = p.events_seen + " events · " + act;
        tailEl.className = "tail";
        tailEl.textContent = head + "\\n\\n" + (p.recent_tail || []).join("\\n");
      }).catch(function () {});
    }
    poll();
    if (streamTimers[runId]) clearInterval(streamTimers[runId]);
    streamTimers[runId] = setInterval(poll, POLL_MS);
  }

  function stopStream(runId) {
    if (streamTimers[runId]) { clearInterval(streamTimers[runId]); delete streamTimers[runId]; }
  }

  function renderRuns(snap) {
    var box = document.getElementById("runs");
    box.innerHTML = "";
    box.appendChild(h("p", "band-title", "runs (" + (snap.runs || []).length + ")"));
    if (!snap.runs || !snap.runs.length) {
      box.appendChild(h("div", "muted", "(no runs found)"));
      return;
    }
    snap.runs.forEach(function (row) {
      var card = h("div", "run" + (row.attention ? " needs" : ""));
      var head = h("div", "run-head");
      head.appendChild(h("span", "run-id", row.run_id));
      head.appendChild(h("span", "badge " + (row.state || ""), row.state));
      var prog = row.progress || {};
      var done = (prog.completed || 0) + (prog.closed_infeasible || 0);
      head.appendChild(h("span", "muted", done + "/" + (prog.total || 0)));
      var cost = (typeof row.cost_usd === "number") ? "$" + row.cost_usd.toFixed(4) : "$?";
      head.appendChild(h("span", "muted", cost));
      head.appendChild(h("span", "grow muted", "last " + fmtAge(row.last_event_age_s) + " ago"));
      if (row.flags && row.flags.length)
        head.appendChild(h("span", "flags", "[" + row.flags.join(", ") + "]"));
      card.appendChild(head);

      var drill = h("div", "drill");
      drill.style.display = open[row.run_id] ? "block" : "none";

      (row.inflight || []).forEach(function (inf) {
        var line = h("div", "inflight");
        line.appendChild(document.createTextNode(inf.task_id + " · " + inf.line + "  "));
        // Only offer the stream affordance when a tailable provider stream actually exists
        // (#137). On the interactive×claude / ENGINE lanes stages run in-session with nothing
        // teeing provider stdout to disk, so the toggle would only ever open an empty panel —
        // show an honest lane note instead of advertising a stream that can't populate.
        if (inf.stream_available) {
          var btn = h("a", "tabbtn", open[row.run_id] ? "hide stream" : "live stream");
          line.appendChild(btn);
          btn.addEventListener("click", function (e) {
            e.stopPropagation();
            if (open[row.run_id]) {
              open[row.run_id] = null; stopStream(row.run_id); drill.style.display = "none";
              btn.textContent = "live stream";
            } else {
              open[row.run_id] = { task: inf.task_id, stage: inf.stage };
              drill.style.display = "block"; btn.textContent = "hide stream";
              drillStream(row.run_id, inf, drill);
            }
          });
        } else {
          line.appendChild(h("span", "muted",
            "in-session lane — no tailable stream; follow events.jsonl / per-stage logs"));
        }
        card.appendChild(line);
      });

      // Re-attach a live poll for a card that was open before this re-render.
      if (open[row.run_id]) {
        var infs = row.inflight || [];
        var match = infs.filter(function (i) {
          return i.task_id === open[row.run_id].task && i.stream_available;
        })[0];
        if (match) drillStream(row.run_id, match, drill);
        else { drill.appendChild(h("div", "muted", "(stream ended)")); stopStream(row.run_id); }
      }
      card.appendChild(drill);
      box.appendChild(card);
    });
  }

  function renderHeader(snap) {
    var hd = snap.header || {};
    var counts = hd.counts || {};
    var bits = Object.keys(counts).sort().map(function (k) { return k + "=" + counts[k]; });
    var usage = hd.usage;
    var usageStr = (usage && usage.five_hour_pct != null)
      ? ("usage 5h " + Math.round(usage.five_hour_pct) + "% / 7d "
         + Math.round(usage.seven_day_pct || 0) + "%")
      : "usage unavailable";
    document.getElementById("summary").textContent =
      (hd.all_quiet ? "ALL QUIET" : ("ATTENTION — " + hd.attention_count + " item(s)"))
      + "  ·  spend $" + (hd.total_spend_usd || 0).toFixed(4)
      + " across " + (hd.shown || 0) + " run(s)  ·  " + usageStr
      + (bits.length ? "  ·  " + bits.join(" ") : "");
    document.getElementById("clock").textContent = hd.generated_at || "";
  }

  function poll() {
    fetch("/api/snapshot").then(function (r) { return r.json(); }).then(function (snap) {
      renderHeader(snap);
      renderAttention(snap);
      renderRuns(snap);
    }).catch(function (e) {
      document.getElementById("summary").textContent = "poll error: " + e;
    });
  }

  poll();
  setInterval(poll, POLL_MS);
})();
</script>
</body>
</html>
"""
