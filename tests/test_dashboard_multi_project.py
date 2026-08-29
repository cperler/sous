"""One board across several runs-roots AND several project adapters (#386).

The cross-run dashboard was cross-*run* only: one runs-root, one `--project`, so two
projects running batches at once (sous under `sous/runs`, family-finance under
`family-finance/runs`) had no combined view — and rendering both through whichever
`--project` was passed would have mis-attributed every row anyway.

The fix is upstream of the board: a run doc now records the adapter it was created with
(`Run.project_ref`), so the board can resolve EACH row's adapter from that row's own doc.
These tests drive the real two-project shape end to end — two roots, two genuinely
different adapters (an importable module and an external directory adapter loaded by
path), one board — plus the degradation paths that must cost one row and never the view.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.dashboard import (
    AdapterUnresolved,
    dashboard_snapshot,
    default_engine_factory,
    discover_runs,
    normalize_roots,
    render_dashboard,
    resolve_run_root,
)
from orchestrator.engine import Engine
from orchestrator.ports.project import ADAPTER_CONTRACT_VERSION
from orchestrator.project_loader import normalize_project_ref
from orchestrator.schemas.status import Run
from orchestrator.status_store import StatusStore
from orchestrator.stream_probe import stages_dir
from tests.conftest import FakeProject, make_result

# The in-repo importable adapter used as "project A" (adapters.project.selfhost's stand-in).
MODULE_ADAPTER = "tests.fakeproject"


# --- builders -------------------------------------------------------------------------


def _engine(run_root) -> Engine:
    run_root.mkdir(parents=True, exist_ok=True)
    return Engine(
        StatusStore(run_root), CostLedger(run_root / "stage-costs.jsonl"), FakeProject()
    )


def _dir_adapter(tmp_path, name: str):
    """An EXTERNAL, project-owned adapter directory (`<repo>/.orchestration`), the form a
    real second project uses. Loaded by path, so it must declare its contract version."""
    d = tmp_path / name / ".orchestration"
    d.mkdir(parents=True)
    (d / "__init__.py").write_text(
        "from tests.conftest import FakeProject\n"
        f"CONTRACT_VERSION = {ADAPTER_CONTRACT_VERSION}\n"
        "\n"
        "class _Config(FakeProject):\n"
        f"    name = {name!r}\n"
        "\n"
        "def get_config():\n"
        "    return _Config()\n",
        encoding="utf-8",
    )
    return d


def _make_run(root, run_id: str, *, project_ref: str | None, **kw) -> Engine:
    """A run in its own `<root>/<run_id>/` store, created with the given adapter ref."""
    eng = _engine(root / run_id)
    eng.create_run(run_id, project_ref=project_ref, **kw)
    eng.add_task(run_id, "t1")
    eng.record(run_id, make_result(eng.next_work(run_id, "t1")))  # deterministic intake
    return eng


def _snapshot(roots, project=None, **kw):
    kw.setdefault("engine_factory", default_engine_factory(project))
    kw.setdefault("clock", lambda: 1_000_000.0)
    return dashboard_snapshot(roots, **kw)


# --- the run doc remembers its adapter (#206 norm, applied to adapter identity) --------


def test_create_run_persists_the_project_ref(tmp_path) -> None:
    eng = _engine(tmp_path / "r1")
    run = eng.create_run("r1", project_ref=MODULE_ADAPTER)
    assert run.project_ref == MODULE_ADAPTER
    # Persisted, not just returned: the next CLI process rebuilds the Engine from defaults.
    assert eng.store.load_run("r1").project_ref == MODULE_ADAPTER


def test_a_directory_ref_is_stored_resolved(tmp_path, monkeypatch) -> None:
    """A directory spec is cwd-relative, so the same adapter reached from two working
    directories would otherwise read as two projects — and a reuse from elsewhere would
    look like a settings mismatch."""
    adapter = _dir_adapter(tmp_path, "family-finance")
    monkeypatch.chdir(tmp_path / "family-finance")
    eng = _engine(tmp_path / "runs" / "r1")
    run = eng.create_run("r1", project_ref=".orchestration")
    assert run.project_ref == str(adapter.resolve())


def test_a_pre_386_run_doc_still_loads(tmp_path) -> None:
    """Additive field, no SCHEMA_VERSION bump: a run doc written before #386 has no
    `project_ref` key at all and must load with the default rather than failing."""
    eng = _engine(tmp_path / "r1")
    eng.create_run("r1")
    path = eng.store.root / "status-r1.json"
    doc = json.loads(path.read_text())
    del doc["project_ref"]  # exactly the pre-change shape
    path.write_text(json.dumps(doc))

    assert Run.model_validate(json.loads(path.read_text())).project_ref is None
    assert eng.store.load_run("r1").project_ref is None


def test_reuse_refuses_a_different_adapter(tmp_path) -> None:
    """`project_ref` joins the immutable reuse settings: the same run id ingested under a
    different adapter is a different run, not an idempotent repeat."""
    from orchestrator.errors import ContractError

    eng = _engine(tmp_path / "r1")
    eng.create_or_reuse_run("r1", project_ref=MODULE_ADAPTER)
    run, created = eng.create_or_reuse_run("r1", project_ref=MODULE_ADAPTER)
    assert created is False and run.project_ref == MODULE_ADAPTER
    with pytest.raises(ContractError, match="project_ref"):
        eng.create_or_reuse_run("r1", project_ref="some.other.adapter")


def test_reuse_adopts_a_ref_a_pre_386_run_does_not_have(tmp_path) -> None:
    """The queue drain re-ingests on every restart. A run created before this field existed
    must not start failing that reuse the moment its driver is upgraded — the requested ref
    fills in the blank (and is stamped, so the board can resolve the run from then on)."""
    eng = _engine(tmp_path / "r1")
    eng.create_run("r1")  # pre-#386 shape: no ref
    run, created = eng.create_or_reuse_run("r1", project_ref=MODULE_ADAPTER)
    assert created is False
    assert run.project_ref == MODULE_ADAPTER
    assert eng.store.load_run("r1").project_ref == MODULE_ADAPTER


def test_normalize_project_ref_leaves_names_and_modules_alone() -> None:
    assert normalize_project_ref("adapters.project.selfhost") == "adapters.project.selfhost"
    assert normalize_project_ref("selfhost") == "selfhost"
    assert normalize_project_ref("  ") is None
    assert normalize_project_ref(None) is None
    # A path-shaped spec that does not exist is kept verbatim — inventing an absolute path
    # for a missing directory would only hide the error `load_project` should report.
    assert normalize_project_ref("../nope/.orchestration") == "../nope/.orchestration"


# --- the real two-project board -------------------------------------------------------


def test_two_roots_two_adapters_one_board(tmp_path) -> None:
    """The shape from the issue: `batch-380-381` under sous/runs (module adapter) beside
    `ff-v1-b13` under family-finance/runs (directory adapter). One board, both runs, each
    row resolved through its OWN persisted ref — with no `--project` passed at all."""
    ff_adapter = _dir_adapter(tmp_path, "family-finance")
    sous_root = tmp_path / "sous" / "runs"
    ff_root = tmp_path / "family-finance" / "runs"
    _make_run(sous_root, "batch-380-381", project_ref=MODULE_ADAPTER)
    _make_run(ff_root, "ff-v1-b13", project_ref=str(ff_adapter))

    snap = _snapshot([sous_root, ff_root])

    rows = {r["run_id"]: r for r in snap["runs"]}
    assert set(rows) == {"batch-380-381", "ff-v1-b13"}
    assert snap["header"]["total_discovered"] == 2
    # Each row is rendered through its own adapter: the labels come from the two DIFFERENT
    # ProjectConfigs, not from one board-wide `--project`.
    assert rows["batch-380-381"]["project"] == "fake"
    assert rows["ff-v1-b13"]["project"] == "family-finance"
    assert rows["ff-v1-b13"]["project_ref"] == str(ff_adapter)
    # Both are readable, real rows — not degraded ones.
    assert not any(r["unreadable"] for r in snap["runs"])

    board = render_dashboard(snap)
    assert "batch-380-381" in board and "ff-v1-b13" in board
    assert "family-finance" in board and "fake" in board


def test_attention_ordering_is_global_across_roots(tmp_path) -> None:
    """A run needing a human outranks a healthy run in ANOTHER project — the sort must not
    be grouped by root, or the board becomes two boards printed in sequence."""
    root_a = tmp_path / "a" / "runs"
    root_b = tmp_path / "b" / "runs"
    # The healthy run is NEWER, so only a global attention-first sort puts B on top.
    eng_b = _make_run(root_b, "b-run", project_ref=MODULE_ADAPTER)
    eng_b.pause_run("b-run", reason="hit the budget ceiling")
    _make_run(root_a, "a-run", project_ref=MODULE_ADAPTER)

    snap = _snapshot([root_a, root_b])

    assert [r["run_id"] for r in snap["runs"]] == ["b-run", "a-run"]
    assert [it["run_id"] for it in snap["attention"]] == ["b-run"]


def test_duplicate_roots_do_not_duplicate_rows(tmp_path) -> None:
    root = tmp_path / "runs"
    _make_run(root, "r1", project_ref=MODULE_ADAPTER)
    snap = _snapshot([root, root, str(root) + "/"])
    assert [r["run_id"] for r in snap["runs"]] == ["r1"]
    assert normalize_roots([root, root]) == [root]
    assert discover_runs([root, root]) == ["r1"]


# --- degradation: one bad run costs one row -------------------------------------------


def test_a_run_without_a_ref_falls_back_to_the_project_argument(tmp_path) -> None:
    """A run doc predating #386 renders normally when `--project` supplies the fallback."""
    root = tmp_path / "runs"
    _make_run(root, "old-run", project_ref=None)

    snap = _snapshot(root, project=MODULE_ADAPTER)

    row = snap["runs"][0]
    assert row["run_id"] == "old-run"
    assert row["unreadable"] is False
    assert row["state"] == "running"
    assert row["project"] == "fake"


def test_an_unresolvable_adapter_degrades_one_row_not_the_board(tmp_path) -> None:
    """One run points at an adapter that no longer loads; the other must still render."""
    root = tmp_path / "runs"
    _make_run(root, "good", project_ref=MODULE_ADAPTER)
    _make_run(root, "broken", project_ref="no.such.adapter.module")

    snap = _snapshot(root)

    rows = {r["run_id"]: r for r in snap["runs"]}
    assert set(rows) == {"good", "broken"}
    assert rows["good"]["state"] == "running"  # the board survives intact
    broken = rows["broken"]
    assert broken["unreadable"] is True
    assert broken["attention"] is True
    # Clearly MARKED, and still identifiable: the row names the ref that failed.
    assert broken["flags"] == ["adapter unresolved: no.such.adapter.module"]
    assert broken["attention_items"][0]["kind"] == "adapter_unresolved"
    assert broken["project"] == "no.such.adapter.module"

    board = render_dashboard(snap)
    assert "good" in board and "broken" in board
    assert "PROJECT ADAPTER UNRESOLVED" in board


def test_a_run_with_no_ref_and_no_fallback_degrades_rather_than_raising(tmp_path) -> None:
    root = tmp_path / "runs"
    _make_run(root, "refless", project_ref=None)

    snap = _snapshot(root)  # no --project fallback at all

    row = snap["runs"][0]
    assert row["unreadable"] is True
    assert row["flags"] == ["adapter unresolved: no project_ref"]
    assert row["project"] == "?"


def test_the_adapter_is_loaded_once_per_spec(tmp_path, monkeypatch) -> None:
    """N runs of one project must not re-import its adapter N times: a directory adapter
    load is a filesystem import, and the board re-renders on every `--watch` tick."""
    root = tmp_path / "runs"
    for i in range(3):
        _make_run(root, f"r{i}", project_ref=MODULE_ADAPTER)

    # The factory imports `load_project` lazily from its own module, so patching the
    # SOURCE is what the resolution actually goes through.
    import orchestrator.project_loader as pl

    loaded: list[str] = []
    real = pl.load_project

    def counting_load(spec: str):
        loaded.append(spec)
        return real(spec)

    monkeypatch.setattr(pl, "load_project", counting_load)
    dashboard_snapshot(
        root, engine_factory=default_engine_factory(None), clock=lambda: 1_000_000.0
    )
    assert loaded.count(MODULE_ADAPTER) == 1, loaded


# --- run-root resolution across several roots -----------------------------------------


def test_resolve_run_root_picks_the_right_root(tmp_path) -> None:
    root_a = tmp_path / "a" / "runs"
    root_b = tmp_path / "b" / "runs"
    _make_run(root_a, "a-run", project_ref=MODULE_ADAPTER)
    _make_run(root_b, "b-run", project_ref=MODULE_ADAPTER)

    assert resolve_run_root([root_a, root_b], "b-run") == root_b / "b-run"
    assert resolve_run_root([root_a, root_b], "nope") is None


def test_resolve_run_root_refuses_to_guess_between_colliding_ids(tmp_path) -> None:
    """Two projects can pick the same run id. Guessing a root would tail the WRONG run's
    stream, so the ambiguity is only resolvable by naming the root."""
    root_a = tmp_path / "a" / "runs"
    root_b = tmp_path / "b" / "runs"
    _make_run(root_a, "batch-1", project_ref=MODULE_ADAPTER)
    _make_run(root_b, "batch-1", project_ref=MODULE_ADAPTER)

    assert resolve_run_root([root_a, root_b], "batch-1") is None
    assert resolve_run_root(
        [root_a, root_b], "batch-1", prefer_root=str(root_b / "batch-1")
    ) == root_b / "batch-1"


# --- the web skin covers the same roots -----------------------------------------------


def test_web_snapshot_and_stream_span_both_roots(tmp_path) -> None:
    from orchestrator.web_dashboard import route_request

    root_a = tmp_path / "a" / "runs"
    root_b = tmp_path / "b" / "runs"
    _make_run(root_a, "a-run", project_ref=MODULE_ADAPTER)
    eng_b = _make_run(root_b, "b-run", project_ref=MODULE_ADAPTER)
    # A live provider stream for the run in the SECOND root — the case a single-root join
    # (`<root>/<run>`) could never have probed.
    eng_b.next_work("b-run", "t1")
    d = stages_dir(root_b / "b-run", "t1")
    d.mkdir(parents=True, exist_ok=True)
    (d / "scope-attempt0.stream.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}}) + "\n"
    )

    kw = dict(
        root=[root_a, root_b],
        engine_factory=default_engine_factory(None),
        clock=lambda: 1_000_000.0,
    )
    status, _ctype, body = route_request("/api/snapshot", {}, **kw)
    assert status == 200
    snap = json.loads(body)
    assert {r["run_id"] for r in snap["runs"]} == {"a-run", "b-run"}

    status, _ctype, body = route_request(
        "/api/stream", {"run": ["b-run"], "task": ["t1"], "stage": ["scope"]}, **kw
    )
    assert status == 200
    assert json.loads(body)["events_seen"] >= 1

    status, _ctype, body = route_request(
        "/api/stream", {"run": ["ghost"], "task": ["t1"]}, **kw
    )
    assert status == 404
    assert "unknown or ambiguous" in json.loads(body)["error"]


def test_factory_raises_adapter_unresolved_for_a_ref_that_will_not_load(tmp_path) -> None:
    factory = default_engine_factory(None)
    with pytest.raises(AdapterUnresolved):
        factory(tmp_path / "runs" / "r1", "no.such.adapter.module")


def test_a_directory_ref_that_will_not_load_is_labelled_by_its_project_dir(tmp_path) -> None:
    """Every project's adapter dir is called `.orchestration`, so a degraded row must name
    the PROJECT directory — `.orchestration` would identify nothing on a shared board."""
    root = tmp_path / "runs"
    _make_run(root, "gone", project_ref="/nowhere/family-finance/.orchestration")

    row = _snapshot(root)["runs"][0]
    assert row["unreadable"] is True
    assert row["project"] == "family-finance"


# --- the CLI's roots resolution -------------------------------------------------------


def test_cli_dashboard_collects_roots_from_flags_and_env(tmp_path, monkeypatch, capsys) -> None:
    """`--also-root` (repeatable) + $ORCHESTRATOR_DASHBOARD_ROOTS, additive with the global
    `--root` and de-duplicated. `--also-root` rather than a repeatable `--root` because a
    subparser `--root` would clobber a global one given before the subcommand."""
    from orchestrator.cli import DASHBOARD_ROOTS_ENV, main

    a, b, c = (tmp_path / n / "runs" for n in ("a", "b", "c"))
    _make_run(a, "a-run", project_ref=MODULE_ADAPTER)
    _make_run(b, "b-run", project_ref=MODULE_ADAPTER)
    _make_run(c, "c-run", project_ref=MODULE_ADAPTER)
    monkeypatch.setenv(DASHBOARD_ROOTS_ENV, str(c))

    # No --project at all: every run carries its own ref.
    rc = main(["--root", str(a), "dashboard", "--also-root", str(b), "--also-root", str(a)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "a-run" in out and "b-run" in out and "c-run" in out
    assert out.count("a-run") == 1  # the repeated root did not duplicate the row


def test_cli_dashboard_needs_at_least_one_root(monkeypatch, capsys) -> None:
    from orchestrator.cli import DASHBOARD_ROOTS_ENV, main

    monkeypatch.delenv(DASHBOARD_ROOTS_ENV, raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["dashboard"])
    assert exc.value.code != 0
    assert "runs-root" in capsys.readouterr().err
