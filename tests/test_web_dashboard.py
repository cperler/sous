"""Read-only web skin over the cross-session board (#94).

These pin the thin HTTP layer WITHOUT a socket or the network: they drive the pure
``route_request`` directly (and ``build_server`` for the bind path) over real ``runs/<id>/``
stores built the same way ``test_dashboard.py`` does (Engine + FakeProject, one store per run).
The assertions match the endpoint output back to ``dashboard_snapshot`` / ``probe_current_stream``
so the JSON shape can't drift from the data functions it wraps.
"""

from __future__ import annotations

import json

from orchestrator.cost_ledger import CostLedger
from orchestrator.dashboard import dashboard_snapshot
from orchestrator.engine import Engine
from orchestrator.schemas.enums import Stage
from orchestrator.status_store import StatusStore
from orchestrator.stream_probe import probe_current_stream, stages_dir
from orchestrator.web_dashboard import INDEX_HTML, build_server, route_request
from tests.conftest import FakeProject, make_result

# --- builders (mirror test_dashboard.py) ------------------------------------------------


def _engine(run_root, **kw) -> Engine:
    run_root.mkdir(parents=True, exist_ok=True)
    return Engine(
        StatusStore(run_root), CostLedger(run_root / "stage-costs.jsonl"), FakeProject(), **kw
    )


def _factory(**kw):
    return lambda run_root: _engine(run_root, **kw)


def _drive_intake(eng, run_id, task_id="t1"):
    eng.create_run(run_id)
    eng.add_task(run_id, task_id)
    eng.record(run_id, make_result(eng.next_work(run_id, task_id)))


def _route(path, query=None, root=None, **kw):
    kw.setdefault("engine_factory", _factory())
    kw.setdefault("clock", lambda: 1_000_000.0)
    return route_request(path, query or {}, root=root, **kw)


# --- /api/snapshot ----------------------------------------------------------------------


def test_snapshot_endpoint_matches_dashboard_snapshot(tmp_path) -> None:
    _drive_intake(_engine(tmp_path / "r1"), "r1")
    status, ctype, body = _route("/api/snapshot", root=tmp_path)
    assert status == 200
    assert ctype.startswith("application/json")
    payload = json.loads(body)
    # Same three-part shape the data function produces (header/attention/runs).
    assert set(payload) == {"header", "attention", "runs"}
    expected = dashboard_snapshot(
        tmp_path, engine_factory=_factory(), clock=lambda: 1_000_000.0
    )
    assert [r["run_id"] for r in payload["runs"]] == [r["run_id"] for r in expected["runs"]]
    assert payload["header"]["shown"] == expected["header"]["shown"]
    assert payload["runs"][0]["state"] == "running"


def test_snapshot_endpoint_passes_snap_kwargs(tmp_path) -> None:
    # Two completed + one running: default caps terminals, show_all reveals all.
    _drive_intake(_engine(tmp_path / "live"), "live")
    for rid in ("done-a", "done-b"):
        eng = _engine(tmp_path / rid)
        eng.create_run(rid)
        eng.add_task(rid, "t1")
        while (w := eng.next_work(rid, "t1")) is not None:
            eng.record(rid, make_result(w))
    _, _, body = _route("/api/snapshot", root=tmp_path, snap_kwargs={"show_all": True})
    assert json.loads(body)["header"]["shown"] == 3


def test_snapshot_endpoint_surfaces_attention(tmp_path) -> None:
    eng = _engine(tmp_path / "r1")
    _drive_intake(eng, "r1")
    eng.pause_run("r1", "human paused")
    _, _, body = _route("/api/snapshot", root=tmp_path)
    payload = json.loads(body)
    assert [a["kind"] for a in payload["attention"]] == ["paused"]
    assert payload["header"]["all_quiet"] is False


# --- /api/stream ------------------------------------------------------------------------


def test_stream_endpoint_returns_probe_for_written_stream(tmp_path) -> None:
    eng = _engine(tmp_path / "r1")
    _drive_intake(eng, "r1")
    w = eng.next_work("r1", "t1")  # dispatch scope, leaves task RUNNING
    assert w.stage is Stage.SCOPE
    d = stages_dir(tmp_path / "r1", "t1")
    d.mkdir(parents=True, exist_ok=True)
    (d / "scope-attempt0.stream.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}}) + "\n"
    )
    status, ctype, body = _route(
        "/api/stream", {"run": ["r1"], "task": ["t1"], "stage": ["scope"]}, root=tmp_path
    )
    assert status == 200
    assert ctype.startswith("application/json")
    payload = json.loads(body)
    expected = probe_current_stream(tmp_path / "r1", "t1", "scope")
    assert payload["events_seen"] == expected["events_seen"] == 1
    assert payload["current_activity"]["tool"] == "Bash"
    assert payload["run"] == "r1" and payload["task"] == "t1" and payload["stage"] == "scope"


def test_stream_endpoint_404_when_no_stream(tmp_path) -> None:
    _drive_intake(_engine(tmp_path / "r1"), "r1")  # intaken, nothing dispatched → no stream
    status, _, body = _route(
        "/api/stream", {"run": ["r1"], "task": ["t1"]}, root=tmp_path
    )
    assert status == 404
    assert json.loads(body)["error"] == "no stream"


def test_stream_endpoint_400_without_run_or_task(tmp_path) -> None:
    status, _, body = _route("/api/stream", {"run": ["r1"]}, root=tmp_path)
    assert status == 400
    assert "required" in json.loads(body)["error"]


# --- static page + routing --------------------------------------------------------------


def test_index_is_self_contained_html(tmp_path) -> None:
    status, ctype, body = _route("/", root=tmp_path)
    assert status == 200
    assert ctype.startswith("text/html")
    html = body.decode("utf-8")
    assert html == INDEX_HTML
    # No external asset host: the page must be fully offline / CSP-safe.
    assert "http://" not in html and "https://" not in html
    assert "/api/snapshot" in html and "/api/stream" in html
    # #137: the live-stream toggle is gated on the row's stream_available flag, and the
    # in-session lanes (no tailable provider stream) get an honest note instead.
    assert "inf.stream_available" in html
    assert "in-session lane" in html


def test_unknown_path_is_404(tmp_path) -> None:
    status, _, body = _route("/nope", root=tmp_path)
    assert status == 404
    assert json.loads(body)["error"] == "not found"


# --- server bind (no serve_forever) -----------------------------------------------------


def test_build_server_binds_and_routes_without_serving(tmp_path) -> None:
    _drive_intake(_engine(tmp_path / "r1"), "r1")
    httpd = build_server(tmp_path, _factory(), host="127.0.0.1", port=0, clock=lambda: 1e6)
    try:
        # An ephemeral port was bound; the handler is wired to route_request.
        assert httpd.server_address[1] > 0
        handler_cls = httpd.RequestHandlerClass
        assert hasattr(handler_cls, "do_GET")
    finally:
        httpd.server_close()
