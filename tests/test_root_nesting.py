"""Regression tests for #81 — ``init-run``/per-run commands auto-nest the store
under a shared ``--root`` so two runs never comingle their files flat, and the
learnings KB lands at the shared parent (not the repo root)."""

from __future__ import annotations

import json

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
