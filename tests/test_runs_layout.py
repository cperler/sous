"""Unit tests for the run-log layout (#523): ``<root>/<project>/<YYYY-MM-DD>/<run>/``.

Every function is pure path logic over a tmp tree, so the layout, id lookup across date
dirs, the legacy ``runs/<run>`` fallback, ambiguity, sanitization and the env override are
each pinned here without an engine."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from orchestrator import runs_layout as rl


def _store(path: Path, run_id: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / f"status-{run_id or path.name}.json").write_text("{}", encoding="utf-8")
    return path


# --- default root / project root -------------------------------------------------------


def test_default_runs_root_is_home_development_runs(monkeypatch) -> None:
    monkeypatch.delenv(rl.RUNS_ROOT_ENV, raising=False)
    assert rl.default_runs_root() == Path("~/Development/runs").expanduser()
    assert "~" not in str(rl.default_runs_root())


def test_default_runs_root_env_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(rl.RUNS_ROOT_ENV, str(tmp_path / "elsewhere"))
    assert rl.default_runs_root() == tmp_path / "elsewhere"
    monkeypatch.setenv(rl.RUNS_ROOT_ENV, "   ")  # blank = unset, not a relative "" path
    assert rl.default_runs_root() == Path("~/Development/runs").expanduser()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("sous", "sous"),
        ("family-finance", "family-finance"),
        ("a/b", "a_b"),
        ("..", "_"),
        ("../../etc", "____etc"),
        ("", "project"),
        ("  ", "project"),
    ],
)
def test_project_runs_root_sanitizes_the_name(tmp_path, name, expected) -> None:
    got = rl.project_runs_root(tmp_path, name)
    assert got == tmp_path / expected
    assert tmp_path in got.parents  # never climbs out of the root


# --- run dir for a creation date -------------------------------------------------------


def test_run_dir_for_accepts_date_iso_string_and_today(tmp_path) -> None:
    assert rl.run_dir_for(tmp_path, "r1", date(2026, 9, 30)) == tmp_path / "2026-09-30" / "r1"
    assert (
        rl.run_dir_for(tmp_path, "r1", "2026-09-30T12:34:56+00:00")
        == tmp_path / "2026-09-30" / "r1"
    )
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert rl.run_dir_for(tmp_path, "r1") == tmp_path / today / "r1"
    with pytest.raises(ValueError):
        rl.run_dir_for(tmp_path, "r1", "not-a-date")


def test_is_date_dirname() -> None:
    assert rl.is_date_dirname("2026-09-30")
    assert not rl.is_date_dirname("2026-9-30")
    assert not rl.is_date_dirname("batch-headless-1")
    assert not rl.is_date_dirname("2026-09-30T00")


# --- id lookup across date dirs --------------------------------------------------------


def test_find_run_dir_scans_date_dirs(tmp_path) -> None:
    _store(tmp_path / "2026-09-01" / "old")
    target = _store(tmp_path / "2026-09-30" / "r1")
    _store(tmp_path / "2026-10-01" / "later")
    assert rl.find_run_dir(tmp_path, "r1") == target
    assert rl.find_run_dir(tmp_path, "nope") is None
    assert rl.find_run_dir(tmp_path, "") is None
    assert rl.find_run_dir(tmp_path / "missing", "r1") is None


def test_find_run_dir_legacy_flat_layout_still_resolves(tmp_path) -> None:
    legacy = _store(tmp_path / "r1")
    assert rl.find_run_dir(tmp_path, "r1") == legacy


def test_find_run_dir_ignores_an_empty_dir_with_the_run_name(tmp_path) -> None:
    # An empty <date>/<run>/ (a failed init, a stray mkdir) is not a run.
    (tmp_path / "2026-09-30" / "r1").mkdir(parents=True)
    assert rl.find_run_dir(tmp_path, "r1") is None
    # ...but a docs-less dir that still holds run logs is (retained after a doc loss).
    (tmp_path / "2026-09-30" / "r1" / "stage-costs.jsonl").write_text("", encoding="utf-8")
    assert rl.find_run_dir(tmp_path, "r1") == tmp_path / "2026-09-30" / "r1"


def test_find_run_dir_refuses_ambiguity(tmp_path) -> None:
    _store(tmp_path / "2026-09-29" / "r1")
    _store(tmp_path / "2026-09-30" / "r1")
    with pytest.raises(rl.AmbiguousRunDirError, match="2026-09-29.*2026-09-30"):
        rl.find_run_dir(tmp_path, "r1")
    # legacy + dated also counts as two claims on one id
    (tmp_path / "2026-09-29" / "r1" / "status-r1.json").unlink()
    _store(tmp_path / "r1")
    with pytest.raises(rl.AmbiguousRunDirError):
        rl.find_run_dir(tmp_path, "r1")


# --- shared root (KB / queue home) -----------------------------------------------------


def test_shared_root_for_dated_and_legacy_layouts(tmp_path) -> None:
    dated = tmp_path / "sous" / "2026-09-30" / "r1"
    assert rl.shared_root_for(dated) == tmp_path / "sous"
    legacy = tmp_path / "runs" / "r1"
    assert rl.shared_root_for(legacy) == tmp_path / "runs"


def test_is_dated_project_root(tmp_path) -> None:
    assert not rl.is_dated_project_root(tmp_path)
    assert not rl.is_dated_project_root(tmp_path / "missing")
    (tmp_path / "2026-09-30").mkdir()
    assert rl.is_dated_project_root(tmp_path)


# --- the cross-run walker --------------------------------------------------------------


def test_iter_run_dirs_walks_dated_legacy_and_multi_project(tmp_path) -> None:
    a = _store(tmp_path / "sous" / "2026-09-30" / "r1")
    b = _store(tmp_path / "sous" / "2026-09-30" / "r2")
    c = _store(tmp_path / "family-finance" / "2026-09-29" / "ff-1")
    d = _store(tmp_path / "legacy-run")  # a pre-#523 runs/<run> dir under the same root
    # noise: a hidden dir, a plain file, an empty dir, and a stages/ tree INSIDE a run
    (tmp_path / ".hidden").mkdir()
    _store(tmp_path / ".hidden" / "ghost")
    (tmp_path / "README").write_text("", encoding="utf-8")
    (tmp_path / "sous" / "2026-09-30" / "empty").mkdir()
    (a / "stages" / "42").mkdir(parents=True)
    (a / "stages" / "42" / "status-fake.json").write_text("{}", encoding="utf-8")

    found = list(rl.iter_run_dirs(tmp_path))
    assert found == sorted([a, b, c, d])
    # a run dir is never descended into, so the nested fake is not a second run
    assert all("stages" not in p.parts for p in found)


def test_iter_run_dirs_honors_max_depth_and_missing_root(tmp_path) -> None:
    _store(tmp_path / "sous" / "2026-09-30" / "r1")
    assert list(rl.iter_run_dirs(tmp_path, max_depth=2)) == []
    assert list(rl.iter_run_dirs(tmp_path / "sous")) == [tmp_path / "sous" / "2026-09-30" / "r1"]
    assert list(rl.iter_run_dirs(tmp_path / "missing")) == []
    assert list(rl.iter_run_dirs(tmp_path, max_depth=0)) == []


def test_is_run_store_dir_shapes(tmp_path) -> None:
    assert not rl.is_run_store_dir(tmp_path)
    assert not rl.is_run_store_dir(tmp_path / "missing")
    (tmp_path / "stages").mkdir()
    assert rl.is_run_store_dir(tmp_path)


# --- opt-in migration planning ---------------------------------------------------------


def _legacy_run(root: Path, run_id: str, created: str | None, project_ref: str | None = None) -> Path:
    import json

    d = root / run_id
    d.mkdir(parents=True)
    doc: dict = {"document_type": "run", "run_id": run_id}
    if created:
        doc["created_at"] = created
    if project_ref:
        doc["project_ref"] = project_ref
    (d / f"status-{run_id}.json").write_text(json.dumps(doc), encoding="utf-8")
    return d


def test_plan_migration_moves_dated_by_created_at_and_skips_the_undecidable(tmp_path) -> None:
    legacy = tmp_path / "runs"
    dest = tmp_path / "dest"
    _legacy_run(legacy, "r1", "2026-09-30T10:00:00+00:00")
    _legacy_run(legacy, "r2", None)  # no created_at → skip
    _legacy_run(legacy, "r3", "2026-09-29T00:00:00+00:00", project_ref="some.adapter")
    (legacy / "r4").mkdir()  # not a run store
    (legacy / "learnings-kb.jsonl").write_text("", encoding="utf-8")
    (legacy / "r5").mkdir()
    (legacy / "r5" / "status-r5.json").write_text("{not json", encoding="utf-8")
    _legacy_run(legacy, "r6", "2026-09-28T00:00:00+00:00")
    (dest / "proj" / "2026-09-28" / "r6").mkdir(parents=True)  # destination taken → skip

    plan = rl.plan_migration(legacy, dest, project_name="proj")
    by_id = {e["run_id"]: e for e in plan}
    assert set(by_id) == {"r1", "r2", "r3", "r5", "r6"}
    assert by_id["r1"]["action"] == "move"
    assert by_id["r1"]["dst"] == str(dest / "proj" / "2026-09-30" / "r1")
    assert by_id["r2"]["action"] == "skip" and "created_at" in by_id["r2"]["reason"]
    assert by_id["r3"]["action"] == "move"  # explicit --project wins over the ref
    assert by_id["r5"]["action"] == "skip" and "run doc" in by_id["r5"]["reason"]
    assert by_id["r6"]["action"] == "skip" and "exists" in by_id["r6"]["reason"]
    # planning moved nothing
    assert (legacy / "r1").is_dir() and not (dest / "proj" / "2026-09-30" / "r1").exists()


def test_plan_migration_names_the_project_from_the_run_doc_ref(tmp_path) -> None:
    legacy = tmp_path / "runs"
    _legacy_run(legacy, "r1", "2026-09-30", project_ref="known")
    _legacy_run(legacy, "r2", "2026-09-30", project_ref="unknown")
    _legacy_run(legacy, "r3", "2026-09-30")
    plan = rl.plan_migration(
        legacy, tmp_path / "dest", name_for_ref=lambda ref: "proj" if ref == "known" else None,
    )
    by_id = {e["run_id"]: e for e in plan}
    assert by_id["r1"]["action"] == "move"
    assert by_id["r2"]["action"] == "skip" and "project name" in by_id["r2"]["reason"]
    assert by_id["r3"]["action"] == "skip"
    assert rl.plan_migration(tmp_path / "missing", tmp_path / "dest") == []
