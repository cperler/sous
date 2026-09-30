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
    return lambda run_root, project_ref=None: _engine(run_root, **kw)


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


# --- /api/snapshot carries the #525 fields the page reads -------------------------------


def test_snapshot_endpoint_carries_redesign_fields(tmp_path) -> None:
    eng = _engine(tmp_path / "r1")
    _drive_intake(eng, "r1")
    eng.next_work("r1", "t1")  # dispatch scope → t1 RUNNING
    eng.pause_run("r1", "human paused")
    payload = json.loads(_route("/api/snapshot", root=tmp_path)[2])
    row = payload["runs"][0]
    # Every per-run block each page section reads is present in the served JSON.
    assert {"tasks", "driver", "cost_breakdown", "recent_events", "terminal"} <= set(row)
    task = row["tasks"][0]
    assert {"task_id", "title", "issue_url", "pr_url", "state", "current_stage",
            "stage_age_s", "model", "activity", "stages", "depends_on",
            "waiting_on"} <= set(task)
    assert task["current_stage"] == "scope" and task["state"] == "running"
    assert {"stage", "status", "attempt", "cost_usd", "unmetered_calls"} <= set(task["stages"][0])
    assert {"state", "summary"} <= set(row["driver"])
    assert {"by_task", "by_stage", "unmetered_calls"} <= set(row["cost_breakdown"])
    assert all("sentence" in e for e in row["recent_events"])
    assert "projects" in payload["header"]
    # The "needs you" band's copyable command rides the attention item itself.
    paused = payload["attention"][0]
    assert paused["kind"] == "paused"
    assert any("unpause" in c["command"] for c in paused["commands"])


# --- /api/stage-file + /api/task-detail ------------------------------------------------


def _stage_files(tmp_path):
    """A run with t1 dispatched at scope and its stage dir populated like a real one."""
    eng = _engine(tmp_path / "r1")
    _drive_intake(eng, "r1")
    eng.next_work("r1", "t1")
    d = stages_dir(tmp_path / "r1", "t1")
    d.mkdir(parents=True, exist_ok=True)
    (d / "scope-attempt0.prompt.txt").write_text("You are the scope stage.\n")
    (d / "scope-attempt0.stream.jsonl").write_text('{"type": "system"}\n')
    return d


def test_stage_file_serves_a_real_prompt(tmp_path) -> None:
    _stage_files(tmp_path)
    status, ctype, body = _route(
        "/api/stage-file",
        {"run": ["r1"], "task": ["t1"], "name": ["scope-attempt0.prompt.txt"]},
        root=tmp_path,
    )
    assert status == 200
    assert ctype.startswith("text/plain")
    assert body == b"You are the scope stage.\n"


def test_stage_file_serves_a_record_the_engine_wrote(tmp_path) -> None:
    # The intake stage record is written by the engine itself, not by the test.
    _stage_files(tmp_path)
    names = [f["name"] for f in json.loads(_route(
        "/api/task-detail", {"run": ["r1"], "task": ["t1"]}, root=tmp_path)[2])["files"]]
    record = next(n for n in names if n.endswith("-intake.json"))
    status, _, body = _route(
        "/api/stage-file", {"run": ["r1"], "task": ["t1"], "name": [record]}, root=tmp_path
    )
    assert status == 200
    assert json.loads(body)["stage"] == "intake"


def test_stage_file_refuses_traversal(tmp_path) -> None:
    _stage_files(tmp_path)
    (tmp_path / "r1" / "secret.txt").write_text("nope")
    for name in ("../../secret.txt", "..", "../status-r1.json", "..%2fsecret.txt"):
        status, _, body = _route(
            "/api/stage-file", {"run": ["r1"], "task": ["t1"], "name": [name]}, root=tmp_path
        )
        assert status == 400, name
        assert b"nope" not in body
    # A task id that walks up out of stages/ is refused too.
    status, _, _ = _route(
        "/api/stage-file",
        {"run": ["r1"], "task": [".."], "name": ["index.md"]},
        root=tmp_path,
    )
    assert status == 400


def test_stage_file_refuses_unknown_names(tmp_path) -> None:
    d = _stage_files(tmp_path)
    (d / "notes.txt").write_text("not a stage file")
    status, _, body = _route(
        "/api/stage-file", {"run": ["r1"], "task": ["t1"], "name": ["notes.txt"]}, root=tmp_path
    )
    assert status == 400
    assert json.loads(body)["error"] == "unknown stage file name"


def test_stage_file_refuses_symlink_out_of_the_stage_dir(tmp_path) -> None:
    d = _stage_files(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (d / "test-attempt0.prompt.txt").symlink_to(outside)
    status, _, body = _route(
        "/api/stage-file",
        {"run": ["r1"], "task": ["t1"], "name": ["test-attempt0.prompt.txt"]},
        root=tmp_path,
    )
    assert status == 400
    assert b"secret" not in body


def test_stage_file_404s(tmp_path) -> None:
    _stage_files(tmp_path)
    missing = _route(
        "/api/stage-file",
        {"run": ["r1"], "task": ["t1"], "name": ["deliver-attempt0.prompt.txt"]},
        root=tmp_path,
    )
    assert missing[0] == 404
    no_run = _route(
        "/api/stage-file",
        {"run": ["nope"], "task": ["t1"], "name": ["scope-attempt0.prompt.txt"]},
        root=tmp_path,
    )
    assert no_run[0] == 404
    no_task = _route(
        "/api/stage-file",
        {"run": ["r1"], "task": ["t9"], "name": ["scope-attempt0.prompt.txt"]},
        root=tmp_path,
    )
    assert no_task[0] == 404


def test_stage_file_400_without_name(tmp_path) -> None:
    status, _, body = _route("/api/stage-file", {"run": ["r1"], "task": ["t1"]}, root=tmp_path)
    assert status == 400
    assert "required" in json.loads(body)["error"]


def test_task_detail_lists_files_and_review_findings(tmp_path) -> None:
    d = _stage_files(tmp_path)
    (d / "notes.txt").write_text("not listed")
    (d / "03-review.json").write_text(json.dumps({"attempt": 0, "structured_output": {
        "approved": False, "issues": [{"title": "old"}], "non_blocking": []}}))
    (d / "07-review.json").write_text(json.dumps({"attempt": 1, "structured_output": {
        "approved": True, "issues": [],
        "non_blocking": [{"title": "rename x", "detail": "clearer", "disposition": "fixup"}]}}))
    status, _, body = _route("/api/task-detail", {"run": ["r1"], "task": ["t1"]}, root=tmp_path)
    assert status == 200
    payload = json.loads(body)
    names = [f["name"] for f in payload["files"]]
    assert "scope-attempt0.prompt.txt" in names and "scope-attempt0.stream.jsonl" in names
    assert "notes.txt" not in names  # only names /api/stage-file would serve are listed
    review = payload["review"]
    # The NEWEST review record wins, not the first one found.
    assert review["file"] == "07-review.json"
    assert review["approved"] is True
    assert review["non_blocking"][0]["title"] == "rename x"


def test_task_detail_survives_a_non_object_review_record(tmp_path) -> None:
    # Valid JSON that is not an object (or a non-object structured_output) must not 500 the
    # panel: the file links still list, and the review reads as empty rather than crashing.
    d = _stage_files(tmp_path)
    (d / "05-review.json").write_text("[]")
    status, _, body = _route("/api/task-detail", {"run": ["r1"], "task": ["t1"]}, root=tmp_path)
    assert status == 200
    payload = json.loads(body)
    assert "scope-attempt0.prompt.txt" in [f["name"] for f in payload["files"]]
    assert payload["review"]["file"] == "05-review.json"
    assert payload["review"]["issues"] == [] and payload["review"]["approved"] is None
    (d / "06-review.json").write_text(json.dumps({"attempt": 2, "structured_output": "oops"}))
    status, _, body = _route("/api/task-detail", {"run": ["r1"], "task": ["t1"]}, root=tmp_path)
    assert status == 200
    review = json.loads(body)["review"]
    assert review["file"] == "06-review.json" and review["attempt"] == 2
    assert review["non_blocking"] == []


def test_task_detail_without_review_or_files(tmp_path) -> None:
    _drive_intake(_engine(tmp_path / "r1"), "r1")
    status, _, body = _route("/api/task-detail", {"run": ["r1"], "task": ["t1"]}, root=tmp_path)
    assert status == 200
    assert json.loads(body)["review"] is None
    status, _, _ = _route("/api/task-detail", {"run": ["r1"], "task": ["zz"]}, root=tmp_path)
    assert status == 404


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


def test_index_has_the_operator_sections_and_filters() -> None:
    # #525: the page is laid out in the operator's order, with client-side filters.
    for section in ("attention", "running", "progress", "cost", "recent"):
        assert f'<section id="{section}">' in INDEX_HTML
    order = [INDEX_HTML.index(f'<section id="{s}">')
             for s in ("attention", "running", "progress", "cost", "recent")]
    assert order == sorted(order)
    for f in ("all", "active", "attention", "finished"):
        assert f'id="filter-{f}" data-filter="{f}"' in INDEX_HTML
    # Detail panel links go through the confined endpoints; light and dark both styled.
    assert "/api/task-detail" in INDEX_HTML and "/api/stage-file" in INDEX_HTML
    assert "prefers-color-scheme: dark" in INDEX_HTML
    # Incremental update (keyed reconcile) rather than wiping each section per poll.
    assert "function reconcile(" in INDEX_HTML
    assert 'getElementById("attention").innerHTML = ""' not in INDEX_HTML


def test_index_renders_a_failed_task_with_its_stage() -> None:
    # #535: a failed task's attention row names the stage that failed, not just "failed".
    assert 'failed: "task failed"' in INDEX_HTML
    assert 'it.kind === "failed"' in INDEX_HTML
    assert '"failed at " + String(it.stage).toUpperCase()' in INDEX_HTML


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
