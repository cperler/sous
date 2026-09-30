"""Regression tests for #81 and #91 — ``init-run``/per-run commands auto-nest the
store under a shared ``--root`` so two runs never comingle their files flat, and the
learnings KB lands at the shared parent (not the repo root).

#91 adds ``--shared-root`` (``force_nest=True`` in ``_resolve_store_root``): the
structural auto-detect heuristic cannot recognize a *fresh*, empty shared ``runs/``
dir (no KB, no sibling stores exist on day one), so the very first run would land flat
without an explicit assertion from the caller. The flag lets the skill pass its
knowledge — "this IS the shared runs-root" — to the engine and forces the nest even
with no markers. The established-flat guard still wins (idempotent for in-progress flat
runs), so the flag is backward-compatible and safe to pass on every engine call."""

from __future__ import annotations

import json

import pytest

from orchestrator.cli import _is_shared_runs_root, _resolve_store_root, main
from orchestrator.learnings_kb import resolve_kb_path


def _run(capsys, *argv) -> dict | None:
    rc = main(list(argv))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out and out != "null" else None


def _seed_shared_root(parent) -> None:
    """Make ``parent`` look like an established runs-root: a prior run's nested
    store plus the cross-run KB (exactly the layout the live footgun hit)."""
    prior = parent / "prior-run"
    prior.mkdir(parents=True)
    (prior / "status-prior-run.json").write_text("{}")
    (parent / "learnings-kb.jsonl").write_text("")


# --- unit: the resolver / detector --------------------------------------------


def test_shared_root_detected_by_kb(tmp_path) -> None:
    (tmp_path / "learnings-kb.jsonl").write_text("")
    assert _is_shared_runs_root(tmp_path, "r1") is True
    assert _resolve_store_root(tmp_path, "r1") == tmp_path / "r1"


def test_shared_root_detected_by_sibling_run_store(tmp_path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "status-other.json").write_text("{}")
    assert _is_shared_runs_root(tmp_path, "r1") is True
    assert _resolve_store_root(tmp_path, "r1") == tmp_path / "r1"


def test_shared_root_detected_by_flat_foreign_status(tmp_path) -> None:
    (tmp_path / "status-someoldrun.json").write_text("{}")
    assert _is_shared_runs_root(tmp_path, "r1") is True


def test_fresh_empty_root_is_used_directly(tmp_path) -> None:
    assert _is_shared_runs_root(tmp_path, "r1") is False
    assert _resolve_store_root(tmp_path, "r1") == tmp_path


def test_own_flat_store_is_not_shared(tmp_path) -> None:
    # A per-run dir already holding THIS run's flat files stays flat (idempotent).
    (tmp_path / "status-r1.json").write_text("{}")
    (tmp_path / "status-r1-#42.json").write_text("{}")
    assert _is_shared_runs_root(tmp_path, "r1") is False
    assert _resolve_store_root(tmp_path, "r1") == tmp_path


def test_existing_nested_dir_is_idempotent(tmp_path) -> None:
    (tmp_path / "r1").mkdir()
    assert _resolve_store_root(tmp_path, "r1") == tmp_path / "r1"


def test_missing_run_id_returns_root(tmp_path) -> None:
    assert _resolve_store_root(tmp_path, None) == tmp_path


# --- #91: --shared-root forces the nest on a fresh runs-root the heuristic misses ---


def test_force_nest_on_fresh_empty_root(tmp_path) -> None:
    # The day-one gap: a fresh runs/ dir has no markers, so auto-detect keeps it flat...
    assert _is_shared_runs_root(tmp_path, "r1") is False
    assert _resolve_store_root(tmp_path, "r1") == tmp_path
    # ...but --shared-root (force_nest) nests it anyway.
    assert _resolve_store_root(tmp_path, "r1", force_nest=True) == tmp_path / "r1"


def test_force_nest_does_not_override_established_flat_run(tmp_path) -> None:
    # An in-progress flat run stays stable even under --shared-root (idempotent).
    (tmp_path / "status-r1.json").write_text("{}")
    assert _resolve_store_root(tmp_path, "r1", force_nest=True) == tmp_path


def test_force_nest_needs_a_run_id(tmp_path) -> None:
    assert _resolve_store_root(tmp_path, None, force_nest=True) == tmp_path


# --- CLI: two runs under one shared parent never interleave --------------------


def test_two_runs_under_shared_root_never_interleave(tmp_path, capsys) -> None:
    parent = tmp_path / "runs"
    _seed_shared_root(parent)

    for run in ("run-a", "run-b"):
        base = ["--root", str(parent), "--run", run, "--project", "tests.fakeproject"]
        _run(capsys, *base, "init-run", "--lane", "full")
        _run(capsys, *base, "add-task", "--task", "#42")

    for run in ("run-a", "run-b"):
        nested = parent / run
        # Every per-run store file lands under <parent>/<run>/, never flat.
        assert (nested / f"status-{run}.json").exists()
        assert (nested / f"status-{run}-#42.json").exists()
        # ...and nothing leaked flat into the shared parent.
        assert not (parent / f"status-{run}.json").exists()
        assert not any(parent.glob(f"status-{run}-*.json"))

    # The two runs' stores stay disjoint directories — no comingling.
    assert (parent / "run-a").is_dir() and (parent / "run-b").is_dir()


def test_learnings_kb_lands_at_shared_parent(tmp_path, capsys, monkeypatch) -> None:
    # conftest pins the KB env override for isolation; drop it here to exercise the
    # real <store-root parent>/learnings-kb.jsonl default the engine relies on.
    monkeypatch.delenv("ORCHESTRATOR_LEARNINGS_KB_PATH", raising=False)
    parent = tmp_path / "runs"
    _seed_shared_root(parent)
    base = ["--root", str(parent), "--run", "run-a", "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")

    store_root = _resolve_store_root(parent, "run-a")
    assert store_root == parent / "run-a"
    # The engine derives the KB from the store-root's PARENT — with the nested
    # store that is the shared runs-root, so the KB stays put (not the repo root).
    assert store_root.parent == parent
    assert resolve_kb_path(store_root.parent) == parent / "learnings-kb.jsonl"


# --- CLI: backward compatibility ---------------------------------------------


def test_fresh_root_still_writes_flat(tmp_path, capsys) -> None:
    # Callers that already point --root at a fresh per-run dir keep the old flat
    # layout (no surprise nesting).
    base = ["--root", str(tmp_path), "--run", "r1", "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")
    _run(capsys, *base, "add-task", "--task", "#42")

    assert (tmp_path / "status-r1.json").exists()
    assert not (tmp_path / "r1").exists()


def test_shared_root_flag_nests_a_fresh_runs_dir(tmp_path, capsys) -> None:
    # #91: on a brand-new runs/ dir (no KB, no sibling stores), --shared-root forces
    # the very first run to nest instead of landing flat — the day-one bootstrap gap.
    parent = tmp_path / "runs"
    base = ["--root", str(parent), "--shared-root",
            "--run", "run-a", "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")
    _run(capsys, *base, "add-task", "--task", "#42")

    nested = parent / "run-a"
    assert (nested / "status-run-a.json").exists()
    assert (nested / "status-run-a-#42.json").exists()
    # Nothing leaked flat into the shared parent.
    assert not (parent / "status-run-a.json").exists()


# --- #101: warn when --shared-root is passed to a command that ignores it -----


def test_shared_root_warns_on_non_engine_command(tmp_path, capsys) -> None:
    # `kb` never routes through _engine(), so --shared-root has no effect there.
    # A mis-positioned flag must not be silently dropped — warn to stderr.
    parent = tmp_path / "runs"
    parent.mkdir()
    (parent / "learnings-kb.jsonl").write_text("")
    assert main(["--root", str(parent), "--shared-root", "kb", "show"]) == 0

    err = capsys.readouterr().err
    assert "--shared-root is ignored by the 'kb' command" in err


def test_shared_root_silent_on_engine_command(tmp_path, capsys) -> None:
    # #101: the flag is legitimate on an engine command (it drives store nesting),
    # so no warning must fire — only the expected nesting note.
    parent = tmp_path / "runs"
    base = ["--root", str(parent), "--shared-root",
            "--run", "run-a", "--project", "tests.fakeproject"]
    assert main([*base, "init-run", "--lane", "full"]) == 0

    err = capsys.readouterr().err
    assert "--shared-root is ignored" not in err


def test_shared_root_silent_on_batch_plan_apply(tmp_path) -> None:
    # `batch-plan apply` is the one subcommand outside the engine block that still
    # builds an Engine, so it consumes --shared-root and must not warn (#101).
    from argparse import Namespace

    from orchestrator.cli import _consumes_shared_root

    assert _consumes_shared_root(Namespace(cmd="batch-plan", batch_cmd="apply"))
    assert not _consumes_shared_root(Namespace(cmd="batch-plan", batch_cmd="validate"))
    assert not _consumes_shared_root(Namespace(cmd="batch-plan", batch_cmd="candidates"))
    assert not _consumes_shared_root(Namespace(cmd="dashboard"))
    assert _consumes_shared_root(Namespace(cmd="init-run"))


def test_trailing_slash_root_does_not_false_positive_nesting_note(tmp_path, capsys) -> None:
    # #90: a trailing slash on a fresh --root normalizes to the same Path as the
    # resolved store root, so no nesting happens and the note must stay silent.
    # (Pre-fix the note compared raw arg-string to normalized Path str and printed.)
    base = ["--root", f"{tmp_path}/", "--run", "r1", "--project", "tests.fakeproject"]
    assert main([*base, "init-run", "--lane", "full"]) == 0

    err = capsys.readouterr().err
    assert "shared runs-root" not in err
    # Store still lands flat at the given root — no surprise nesting from the slash.
    assert (tmp_path / "status-r1.json").exists()
    assert not (tmp_path / "r1").exists()


# --- #523: the default runs location is OUTSIDE the project, dated, found by id ---------


def _today() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d")


def test_no_root_lands_under_the_default_dated_layout(tmp_path, capsys, monkeypatch) -> None:
    """A fresh run with NO --root lands at <runs root>/<project name>/<today>/<run>/ and
    writes nothing under <cwd>/runs/. The project name is the adapter's (`fake`), not the
    checkout's directory name."""
    monkeypatch.chdir(tmp_path)  # a cwd with no runs/ at all — it must stay that way
    default_root = tmp_path / "default-runs-root"  # conftest pins ORCHESTRATOR_RUNS_ROOT here
    base = ["--run", "run-a", "--project", "tests.fakeproject"]
    assert main([*base, "init-run", "--lane", "full"]) == 0
    err = capsys.readouterr().err
    assert "run store at" in err  # never silent about where the store landed

    store = default_root / "fake" / _today() / "run-a"
    assert (store / "status-run-a.json").exists()
    assert not (tmp_path / "runs").exists()
    assert not any(p.name.startswith("status-") for p in default_root.iterdir())


def test_no_root_per_run_commands_find_the_run_by_id_alone(tmp_path, capsys, monkeypatch) -> None:
    default_root = tmp_path / "default-runs-root"
    base = ["--run", "run-a", "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")
    # Move the run to an OLDER date dir: a later command must find it by scanning, not by
    # recomputing today's path.
    created = default_root / "fake" / _today() / "run-a"
    older = default_root / "fake" / "2026-01-01" / "run-a"
    older.parent.mkdir(parents=True)
    created.rename(older)

    _run(capsys, *base, "add-task", "--task", "#42")
    status = _run(capsys, *base, "status")
    assert status["run_id"] == "run-a"
    assert (older / "status-run-a-#42.json").exists()
    assert not created.exists()  # nothing re-created under today's date


def test_run_doc_records_its_resolved_run_dir(tmp_path, capsys) -> None:
    """The store dir is persisted on the Run doc (#206 norm), not recomputed from engine
    defaults — a later process on another day or with another default root can read it."""
    base = ["--run", "run-a", "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")
    store = tmp_path / "default-runs-root" / "fake" / _today() / "run-a"
    doc = json.loads((store / "status-run-a.json").read_text())
    assert doc["run_dir"] == str(store.resolve())


def test_explicit_root_still_overrides_the_default(tmp_path, capsys) -> None:
    root = tmp_path / "custom"
    base = ["--root", str(root), "--shared-root", "--run", "run-a",
            "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")
    assert (root / "run-a" / "status-run-a.json").exists()
    assert not (tmp_path / "default-runs-root").exists()


def test_explicit_root_at_a_dated_project_root_resolves_and_nests_by_date(
    tmp_path, capsys,
) -> None:
    """--root at <runs root>/<project> (the dated layout) finds an existing run under its
    date dir and puts a fresh one under today's, never flat beside the date dirs."""
    project_root = tmp_path / "default-runs-root" / "fake"
    _run(capsys, "--run", "run-a", "--project", "tests.fakeproject", "init-run", "--lane", "full")
    existing = project_root / _today() / "run-a"
    assert _resolve_store_root(project_root, "run-a") == existing

    base = ["--root", str(project_root), "--run", "run-b", "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")
    assert (project_root / _today() / "run-b" / "status-run-b.json").exists()
    assert not (project_root / "run-b").exists()
    assert not (project_root / "status-run-b.json").exists()


def test_legacy_runs_root_still_resolves_by_id(tmp_path, capsys) -> None:
    parent = tmp_path / "runs"
    _seed_shared_root(parent)
    base = ["--root", str(parent), "--run", "run-a", "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")
    assert _resolve_store_root(parent, "run-a") == parent / "run-a"
    # and a dated resolver over the same root agrees
    from orchestrator.runs_layout import find_run_dir

    assert find_run_dir(parent, "run-a") == parent / "run-a"


def test_ambiguous_run_id_exits_with_both_paths_named(tmp_path, capsys) -> None:
    project_root = tmp_path / "default-runs-root" / "fake"
    for day in ("2026-01-01", "2026-01-02"):
        d = project_root / day / "run-a"
        d.mkdir(parents=True)
        (d / "status-run-a.json").write_text("{}")
    with pytest.raises(SystemExit) as exc:
        main(["--run", "run-a", "--project", "tests.fakeproject", "status"])
    assert "2026-01-01" in str(exc.value) and "2026-01-02" in str(exc.value)


def test_shared_root_without_root_warns(tmp_path, capsys) -> None:
    assert main(["--shared-root", "--run", "run-a", "--project", "tests.fakeproject",
                 "init-run", "--lane", "full"]) == 0
    assert "--shared-root has no effect without --root" in capsys.readouterr().err


def test_learnings_kb_lands_at_the_project_level_in_the_dated_layout(
    tmp_path, monkeypatch,
) -> None:
    """The KB is about a PROJECT's runs, so in the dated layout it sits above the date dirs
    (<runs root>/<project>/learnings-kb.jsonl); a legacy runs/<run> store keeps its parent."""
    from orchestrator.cost_ledger import CostLedger
    from orchestrator.engine import Engine
    from orchestrator.status_store import StatusStore
    from tests.conftest import FakeProject

    monkeypatch.delenv("ORCHESTRATOR_LEARNINGS_KB_PATH", raising=False)
    dated = tmp_path / "root" / "fake" / "2026-09-30" / "r1"
    eng = Engine(StatusStore(dated), CostLedger(dated / "stage-costs.jsonl"), FakeProject())
    assert eng._learnings_kb_path() == tmp_path / "root" / "fake" / "learnings-kb.jsonl"
    legacy = tmp_path / "runs" / "r1"
    eng = Engine(StatusStore(legacy), CostLedger(legacy / "stage-costs.jsonl"), FakeProject())
    assert eng._learnings_kb_path() == tmp_path / "runs" / "learnings-kb.jsonl"


def test_kb_and_tail_default_to_the_project_root(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("ORCHESTRATOR_LEARNINGS_KB_PATH", raising=False)  # conftest pin
    project_root = tmp_path / "default-runs-root" / "fake"
    out = _run(capsys, "--project", "tests.fakeproject", "kb", "show")
    assert out["path"] == str(project_root / "learnings-kb.jsonl")
    _run(capsys, "--run", "run-a", "--project", "tests.fakeproject", "init-run", "--lane", "full")
    assert main(["--run", "run-a", "--project", "tests.fakeproject", "tail", "#42"]) == 0
    assert "no live stream" in capsys.readouterr().out


@pytest.mark.parametrize("cmd", [["status"], ["abandon", "--task", "#42", "--reason", "x"],
                                 ["add-task", "--task", "#42"],
                                 ["tail", "#42"]])
def test_unknown_run_id_without_root_creates_no_directory(tmp_path, capsys, cmd) -> None:
    """A per-run command on an unknown id (a typo) exits with a clear 'not found' error
    and leaves NOTHING under the default runs root — only init-run may create today's
    dated dir. Without this, every miss left an empty <today>/<typo>/ in ~/Development."""
    default_root = tmp_path / "default-runs-root"
    with pytest.raises(SystemExit) as exc:
        main(["--run", "typo", "--project", "tests.fakeproject", *cmd])
    assert "run 'typo' not found under" in str(exc.value)
    assert not default_root.exists() or not any(default_root.rglob("typo"))


def test_unknown_run_id_miss_leaves_existing_project_root_untouched(tmp_path, capsys) -> None:
    project_root = tmp_path / "default-runs-root" / "fake"
    _run(capsys, "--run", "run-a", "--project", "tests.fakeproject", "init-run", "--lane", "full")
    before = sorted(p.relative_to(project_root) for p in project_root.rglob("*"))
    with pytest.raises(SystemExit):
        main(["--run", "typo", "--project", "tests.fakeproject", "status"])
    assert sorted(p.relative_to(project_root) for p in project_root.rglob("*")) == before
