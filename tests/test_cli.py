"""CLI smoke test — drives the supervisor's Bash interface end to end."""

from __future__ import annotations

import json

import pytest

from orchestrator.cli import main
from orchestrator.errors import ContractError
from orchestrator.schemas.work import WorkItem
from tests.conftest import make_result


def _run(capsys, *argv) -> dict | None:
    rc = main(list(argv))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out and out != "null" else None


def test_cli_drives_a_task_to_completion(tmp_path, capsys) -> None:
    root = str(tmp_path)
    base = ["--root", root, "--run", "run1", "--project", "tests.fakeproject"]

    _run(capsys, *base, "init-run", "--lane", "full")
    _run(capsys, *base, "add-task", "--task", "#42")

    # supervisor loop: next -> (shim would run agent) -> record. `next` drains the
    # deterministic intake stage in-process, so the supervisor only sees model stages.
    stages_recorded = []
    for _ in range(10):  # safety bound
        work = _run(capsys, *base, "next", "--task", "#42")
        if work is None:
            break
        from orchestrator.schemas.work import WorkItem

        wi = WorkItem.model_validate(work)
        result_file = tmp_path / "result.json"
        result_file.write_text(make_result(wi).model_dump_json())
        outcome = _run(capsys, *base, "record", "--result", str(result_file))
        stages_recorded.append(outcome["stage"])
        assert outcome["lane_attributed"] is True

    assert stages_recorded == ["scope", "implement", "test", "deliver", "review"]

    status = _run(capsys, *base, "status")
    assert status["tasks"]["#42"]["state"] == "completed"
    assert status["lane_audit"]["clean"] is True
    assert status["lane_audit"]["total_calls"] == 6
    assert status["cost"]["total_invocations"] == 6
    # #169: the cost block carries the per-effort + ENGINE-lane rollups. The deterministic
    # intake stage ran on the ENGINE lane, so its $0 invocation is attributed there.
    assert "by_effort_spend" in status["cost"]
    assert status["cost"]["engine_lane"]["invocations"] >= 1
    assert status["cost"]["engine_lane"]["cost_usd"] == 0.0

    report = _run(capsys, *base, "cost-report")
    assert set(report["by_stage"]) == {"intake", "scope", "implement", "test", "deliver", "review"}
    assert "net_win_usd" in report["session_reuse"]
    assert (tmp_path / "cost-report.md").exists()  # written at run finalize

    # #169/#167: --by-effort emits the human-readable table (Markdown), not raw JSON
    rc = main([*base, "cost-report", "--by-effort"])
    assert rc == 0
    by_effort_out = capsys.readouterr().out
    assert "# Cost by effort — run1" in by_effort_out
    assert "| Stage | Effort | Model | Calls |" in by_effort_out


def test_cli_next_resume_reemits_leased_item_and_record_succeeds(tmp_path, capsys) -> None:
    # #50: a supervisor got a WorkItem then crashed before recording — the dispatch lease
    # is held. `next --resume` must re-emit that leased item (byte-identical in the fields
    # that define the work) so the supervisor recovers without hand-editing state, and a
    # record() of the re-emitted item must still validate against the (re-bound) lease.
    base = ["--root", str(tmp_path), "--run", "run1", "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")
    _run(capsys, *base, "add-task", "--task", "#42")

    # first `next` drains the deterministic intake and emits the first MODEL WorkItem
    # (scope); the lease is now held. Simulate a crash: do NOT record it.
    first = _run(capsys, *base, "next", "--task", "#42")
    assert first is not None and first["stage"] == "scope"

    # a plain `next` now refuses — the lease is held (proves resume is the only recovery).
    with pytest.raises(ContractError):
        main([*base, "next", "--task", "#42"])

    # `next --resume` re-emits the same crashed stage, byte-identical where it matters.
    resumed = _run(capsys, *base, "next", "--task", "#42", "--resume")
    assert resumed is not None and resumed["stage"] == "scope"
    assert resumed["content_hash"] == first["content_hash"]  # same work-defining fields

    # recording the re-emitted item validates against the re-bound lease and advances.
    wi = WorkItem.model_validate(resumed)
    result_file = tmp_path / "result.json"
    result_file.write_text(make_result(wi).model_dump_json())
    outcome = _run(capsys, *base, "record", "--result", str(result_file))
    assert outcome["stage"] == "scope" and outcome["lane_attributed"] is True


def test_cli_record_refuses_a_mismatched_content_hash_as_json(tmp_path, capsys) -> None:
    # #311: the supervisor reads this command's JSON. A result whose content_hash does not
    # answer the outstanding dispatch must come back as a non-zero, machine-readable
    # refusal — not a traceback with nothing on stdout (which reads as a transport hiccup
    # rather than "your result was rejected").
    base = ["--root", str(tmp_path), "--run", "run1", "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")
    _run(capsys, *base, "add-task", "--task", "#42")
    work = _run(capsys, *base, "next", "--task", "#42")

    wi = WorkItem.model_validate(work)
    garbled = make_result(wi).model_copy(update={"content_hash": wi.content_hash[:16]})
    result_file = tmp_path / "result.json"
    result_file.write_text(garbled.model_dump_json())

    rc = main([*base, "record", "--result", str(result_file)])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False and payload["recorded"] is False
    assert "content_hash" in payload["error"]
    assert payload["task_id"] == "#42" and payload["stage"] == "scope"
    # …and the refusal is in the run's durable log, not just this process's stdout.
    events = (tmp_path / "events.jsonl").read_text()
    assert '"type":"result_rejected"' in events


def test_cli_next_resume_with_nothing_leased_errors_clearly(tmp_path, capsys) -> None:
    # #50: --resume is ONLY for recovering a held lease. On a fresh task with nothing
    # outstanding there is nothing to resume — fail loudly rather than silently minting a
    # fresh dispatch (which could double-run a stage whose supervisor is still alive).
    base = ["--root", str(tmp_path), "--run", "run1", "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")
    _run(capsys, *base, "add-task", "--task", "#42")

    with pytest.raises(ContractError, match="nothing leased"):
        main([*base, "next", "--task", "#42", "--resume"])


def test_cli_next_terminates_when_deterministic_setup_fails(tmp_path, capsys) -> None:
    # Regression: the `next` drain runs the deterministic intake in-process. If that stage
    # persistently fails, `next` must terminate (task -> FAILED, returns null), NOT loop
    # forever re-dispatching the failed stage.
    base = ["--root", str(tmp_path), "--run", "r", "--project", "tests.failsetup"]
    _run(capsys, *base, "init-run", "--lane", "full")
    _run(capsys, *base, "add-task", "--task", "#7")

    work = _run(capsys, *base, "next", "--task", "#7")  # drains intake; must not hang

    assert work is None  # terminated instead of re-dispatching a failed task forever
    status = _run(capsys, *base, "status")
    assert status["tasks"]["#7"]["state"] == "failed"


def test_cli_watch_exits_on_terminal_run(tmp_path, capsys) -> None:
    # `watch` polls to a terminal state and prints a final summary. Driving the task to
    # completion first means the run is already terminal, so watch returns on the first
    # poll without ever sleeping (a passing test can't block on real time).
    base = ["--root", str(tmp_path), "--run", "w1", "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")
    _run(capsys, *base, "add-task", "--task", "#9")
    for _ in range(10):
        work = _run(capsys, *base, "next", "--task", "#9")
        if work is None:
            break
        from orchestrator.schemas.work import WorkItem

        wi = WorkItem.model_validate(work)
        rf = tmp_path / "result.json"
        rf.write_text(make_result(wi).model_dump_json())
        _run(capsys, *base, "record", "--result", str(rf))

    summary = _run(capsys, *base, "watch", "--interval", "1")
    assert summary["watch"] == "done"
    assert summary["run_state"] == "completed"
    assert summary["progress"]["completed"] == 1


# --- #66: the `tail` command (raw stream tail; no engine/project needed) ---------------

def test_cli_tail_prints_recent_lines(tmp_path, capsys) -> None:
    from orchestrator.stream_probe import stages_dir, stream_filename

    d = stages_dir(tmp_path, "#42")
    d.mkdir(parents=True)
    (d / stream_filename("implement", 0)).write_text("l1\nl2\nl3\n")

    rc = main(["--root", str(tmp_path), "tail", "#42", "--lines", "2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "l2" in out and "l3" in out and "l1" not in out  # last 2 lines only


def test_cli_tail_no_stream_says_so_cleanly(tmp_path, capsys) -> None:
    # An interactive/ENGINE-lane task (or one that never dispatched) has no stream.
    rc = main(["--root", str(tmp_path), "tail", "#7"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no live stream" in out


def test_cli_tail_follow_prints_appended_lines(tmp_path, capsys, monkeypatch) -> None:
    import time as _time

    from orchestrator.stream_probe import stages_dir, stream_filename

    d = stages_dir(tmp_path, "#42")
    d.mkdir(parents=True)
    f = d / stream_filename("implement", 0)
    f.write_text("x1\n")

    state = {"n": 0}

    def fake_sleep(_interval) -> None:  # the injected follow sleeper
        state["n"] += 1
        if state["n"] == 1:
            with f.open("a", encoding="utf-8") as fh:
                fh.write("x2\n")
        else:
            raise KeyboardInterrupt  # the CLI catches this to end the follow

    monkeypatch.setattr(_time, "sleep", fake_sleep)
    rc = main(["--root", str(tmp_path), "tail", "#42", "--follow", "--interval", "0"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "x1" in out and "x2" in out  # initial tail + the appended line


def test_cli_enqueue_and_run_queue(tmp_path, capsys) -> None:
    # #1: the `enqueue` producer appends batches (no engine), and `run-queue` drains them
    # in-process (headless) to terminal, deriving each run id from the batch's enqueued_at.
    # TWO batches SHARING a task id prove the #281 isolation model: each derived run gets
    # its own self-contained store nested under --root, so ledgers and stage artifacts
    # never collide and per-run reporting counts only that run's calls.
    queue = tmp_path / "queue.json"
    out = _run(capsys, "enqueue", "--queue-file", str(queue),
               "--tasks", "#42,#43", "--branch", "batch-a")
    assert out["ok"] is True
    assert out["enqueued"]["tasks"] == ["#42", "#43"]
    assert out["enqueued"]["branch"] == "batch-a"
    out2 = _run(capsys, "enqueue", "--queue-file", str(queue),
                "--tasks", "#42", "--branch", "batch-b")  # #42 appears in BOTH batches
    assert out2["ok"] is True

    # run-queue drains both batches in-process, one derived run each.
    # (FakeProject has no real headless model, so the runs terminate failed — the point of
    # this CLI test is the enqueue->claim->ingest->drive->complete WIRING; the
    # scripted-lane e2e in test_queue_file covers a green completion.)
    summary = _run(capsys, "--root", str(tmp_path), "--project", "tests.fakeproject",
                   "run-queue", "--queue-file", str(queue))
    assert summary["batches_processed"] == 2
    assert summary["runs_created"] == 2
    assert summary["runs"][0]["tasks"] == ["#42", "#43"]
    assert summary["runs"][1]["tasks"] == ["#42"]
    assert all(r["final_state"] in ("completed", "failed") for r in summary["runs"])

    # the queue drained to empty (each head was completed after its run went terminal).
    assert json.loads(queue.read_text()) == []

    # #281: two SELF-CONTAINED run dirs nested under --root — own run doc, own ledger,
    # own stages/ tree for the shared task id (no cross-run overwrite possible).
    rid1, rid2 = (r["run_id"] for r in summary["runs"])
    assert rid1 != rid2
    for rid in (rid1, rid2):
        run_root = tmp_path / rid
        assert (run_root / f"status-{rid}.json").is_file()  # per-run store, nested
        rows = [json.loads(line) for line in
                (run_root / "stage-costs.jsonl").read_text().splitlines() if line.strip()]
        assert rows and all(row["run_id"] == rid for row in rows)  # ledger holds ONLY its run

    # per-run status (rebuilt CLI engine over the nested root) reports only its own calls.
    for rid in (rid1, rid2):
        status = _run(capsys, "--root", str(tmp_path), "--run", rid,
                      "--project", "tests.fakeproject", "status")
        rows = [json.loads(line) for line in
                (tmp_path / rid / "stage-costs.jsonl").read_text().splitlines() if line.strip()]
        assert status["cost"]["total_invocations"] == len(rows)
        assert status["lane_audit"]["total_calls"] == len(rows)


def test_cli_run_queue_releases_a_stale_head_claim(tmp_path, capsys) -> None:
    from orchestrator.queue_file import _HAVE_FCNTL, QueueFile, consumer_guard

    queue = tmp_path / "queue.json"
    _run(capsys, "enqueue", "--queue-file", str(queue), "--tasks", "#42")
    stale = QueueFile(queue).claim_head("retired-cron", "queue-stale")["claim"]

    result = _run(
        capsys, "run-queue", "--queue-file", str(queue), "--release-claim"
    )

    assert result == {"ok": True, "released": stale}
    assert "claim" not in QueueFile(queue).peek_head()

    live = QueueFile(queue).claim_head("live-cron", "queue-live")["claim"]
    if _HAVE_FCNTL:
        with consumer_guard(QueueFile(queue), "live-cron"):
            rc = main(["run-queue", "--queue-file", str(queue), "--release-claim"])
        assert rc == 1
        refusal = json.loads(capsys.readouterr().out)
        assert refusal["ok"] is False and "live-cron" in refusal["error"]
        assert QueueFile(queue).peek_head()["claim"] == live

    forced = _run(
        capsys, "run-queue", "--queue-file", str(queue), "--release-claim", "--force"
    )
    assert forced == {"ok": True, "released": live}
    assert "claim" not in QueueFile(queue).peek_head()


# --- #94: `dashboard --serve` wires the read-only web skin (no blocking serve_forever) ---

def test_cli_dashboard_serve_wires_web_dashboard(tmp_path, capsys, monkeypatch) -> None:
    from orchestrator import web_dashboard

    captured = {}

    def fake_serve(root, factory, **kw) -> None:  # stands in for the blocking serve()
        captured["root"] = root
        captured["factory"] = factory
        captured["kw"] = kw
        if kw.get("on_ready"):
            kw["on_ready"]("http://127.0.0.1:8787/")  # exercise the CLI's ready-print

    monkeypatch.setattr(web_dashboard, "serve", fake_serve)
    rc = main([
        "--root", str(tmp_path), "--project", "tests.fakeproject",
        "dashboard", "--serve", "--port", "9191", "--host", "0.0.0.0",
    ])
    assert rc == 0
    assert captured["root"] == str(tmp_path)
    assert captured["kw"]["port"] == 9191 and captured["kw"]["host"] == "0.0.0.0"
    assert "stale_after_s" in captured["kw"]["snap_kwargs"]
    assert callable(captured["kw"]["usage_reader"])
    assert "serving at" in capsys.readouterr().out


# --- #121: `dashboard --serve` and `--watch` are mutually exclusive (no silent win) ---

def test_cli_dashboard_serve_and_watch_are_mutually_exclusive(tmp_path, capsys) -> None:
    # Passing both used to let --serve silently win; argparse must now error out.
    with pytest.raises(SystemExit) as exc:
        main([
            "--root", str(tmp_path), "--project", "tests.fakeproject",
            "dashboard", "--serve", "--watch",
        ])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "not allowed with" in err and "--serve" in err and "--watch" in err


def test_cli_dashboard_watch_alone_still_parses(tmp_path, capsys, monkeypatch) -> None:
    # Each mode flag on its own must still reach its dispatch branch.
    from orchestrator import dashboard

    captured = {}

    def fake_render_watch(root, **kw) -> None:  # stands in for the blocking loop
        captured["root"] = root
        captured["interval"] = kw.get("interval")

    monkeypatch.setattr(dashboard, "render_watch", fake_render_watch)
    rc = main([
        "--root", str(tmp_path), "--project", "tests.fakeproject",
        "dashboard", "--watch", "--interval", "1",
    ])
    assert rc == 0
    assert captured["root"] == str(tmp_path)
    assert captured["interval"] == 1
