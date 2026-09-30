"""Where run logs live on disk (#523) — pure path logic, no I/O beyond directory scans.

The default runs location is OUTSIDE the project tree, grouped by project and creation
date::

    ~/Development/runs/<project_name>/<YYYY-MM-DD>/<run_id>/

``<project_name>`` is the project adapter's ``name`` (never the checkout's directory
name), the date is the run's creation date fixed once at ``init-run``, and ``<run_id>/``
is the run's own ``StatusStore`` root — the same layout it always had, one level deeper.
Per-project shared files (``learnings-kb.jsonl``, a queue file) live at the project level,
``<root>/<project_name>/``, since they describe that project's runs and not one run.

The legacy layout (``<project>/runs/<run_id>/``, or a flat per-run ``--root``) keeps
working: every resolver here treats a run found directly under a root as valid too, so a
pre-#523 ``--root <project>/runs --run X`` still resolves, and the cross-run walkers
(dashboard, panel-report, kb backfill) see both shapes under one root.

This module is engine-owned and imports nothing from ``adapters``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path

#: Env override for the top-level runs root (all projects). Same "name it once in the
#: environment" habit as ``ORCHESTRATOR_DASHBOARD_ROOTS`` / ``ORCHESTRATOR_ENGINE_PROJECT``.
RUNS_ROOT_ENV = "ORCHESTRATOR_RUNS_ROOT"

#: The built-in default when the env var is unset.
DEFAULT_RUNS_ROOT = "~/Development/runs"

#: The date level of the layout: a calendar day, zero-padded, so ``sorted()`` is
#: chronological and a scan can tell a date dir from a legacy run dir by shape alone.
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Depth of ``<project>/<date>/<run>`` below the top-level runs root; the deepest a
#: cross-project walk ever needs to look.
DEFAULT_WALK_DEPTH = 3


class AmbiguousRunDirError(LookupError):
    """More than one run directory under a root claims the same run id.

    Raised instead of picking one: a wrong-dir read would silently show another run's
    state. The message names every match so the operator can pass ``--root`` at the
    right one (or move the stray)."""


def default_runs_root() -> Path:
    """The top-level runs root: ``$ORCHESTRATOR_RUNS_ROOT`` else ``~/Development/runs``,
    ``~`` expanded. Not resolved/created here — callers create what they write."""
    raw = os.environ.get(RUNS_ROOT_ENV, "").strip() or DEFAULT_RUNS_ROOT
    return Path(raw).expanduser()


def safe_project_dirname(name: str) -> str:
    """The filesystem-safe directory component for a project name: path separators and
    ``..`` collapse to ``_`` so a hostile or odd adapter name cannot climb out of the runs
    root; an empty name becomes ``project``."""
    cleaned = name.replace("/", "_").replace("\\", "_").replace("..", "_").strip()
    return cleaned or "project"


def project_runs_root(root: str | Path, project_name: str) -> Path:
    """``<root>/<project_name>`` — the per-project level that holds the date dirs and the
    project's shared files (learnings KB, queue file)."""
    return Path(root) / safe_project_dirname(project_name)


def is_date_dirname(name: str) -> bool:
    """True for a ``YYYY-MM-DD`` directory name (the layout's date level)."""
    return bool(_DATE_DIR.match(name))


def today_dirname(now: datetime | None = None) -> str:
    """The date-level dir name for a run created now (UTC — matches the run doc's
    ``created_at``, which is UTC ISO-8601)."""
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d")


def run_dir_for(project_root: str | Path, run_id: str, created: str | date | None = None) -> Path:
    """``<project_root>/<YYYY-MM-DD>/<run_id>`` for a run created on ``created`` (a
    ``date``, an ISO-8601 date/datetime string such as the run doc's ``created_at``, or
    None for today UTC)."""
    if created is None:
        day = today_dirname()
    elif isinstance(created, date):
        day = created.strftime("%Y-%m-%d")
    else:
        day = str(created)[:10]
        if not is_date_dirname(day):
            raise ValueError(f"not an ISO date: {created!r}")
    return Path(project_root) / day / run_id


def is_run_store_dir(path: Path) -> bool:
    """True when ``path`` is a run's own store dir: it holds a ``status-*.json`` doc, or the
    per-stage log tree / cost ledger a run leaves behind even when its docs are gone."""
    if not path.is_dir():
        return False
    try:
        if any(path.glob("status-*.json")):
            return True
    except OSError:  # pragma: no cover - defensive
        return False
    return (path / "stages").is_dir() or (path / "stage-costs.jsonl").is_file()


def is_dated_project_root(root: str | Path) -> bool:
    """True when ``root`` already holds the dated layout (any ``YYYY-MM-DD`` child dir), so
    a fresh run given ``--root`` at a project root nests under today's date rather than
    landing flat beside the date dirs."""
    root = Path(root)
    if not root.is_dir():
        return False
    try:
        return any(child.is_dir() and is_date_dirname(child.name) for child in root.iterdir())
    except OSError:  # pragma: no cover - defensive
        return False


def find_run_dir(root: str | Path, run_id: str) -> Path | None:
    """The store dir for ``run_id`` beneath a project root, resolved by id alone.

    Scans the date-level dirs (``<root>/<YYYY-MM-DD>/<run_id>/``) — the date is fixed at
    creation, so a later ``status``/``watch``/``abandon`` cannot recompute it and must
    look. Also accepts the legacy ``<root>/<run_id>/`` shape so one resolver serves both
    layouts. Returns None when nothing matches; raises ``AmbiguousRunDirError`` when more
    than one dir claims the id (never guesses).
    """
    root = Path(root)
    if not root.is_dir() or not run_id:
        return None
    matches: list[Path] = []
    legacy = root / run_id
    if is_run_store_dir(legacy):
        matches.append(legacy)
    try:
        children = sorted(root.iterdir())
    except OSError:  # pragma: no cover - defensive
        return None
    for child in children:
        if not child.is_dir() or not is_date_dirname(child.name):
            continue
        candidate = child / run_id
        if is_run_store_dir(candidate):
            matches.append(candidate)
    if not matches:
        return None
    if len(matches) > 1:
        listed = ", ".join(str(m) for m in matches)
        raise AmbiguousRunDirError(
            f"run {run_id!r} found in more than one place under {root}: {listed}"
        )
    return matches[0]


def shared_root_for(run_dir: str | Path) -> Path:
    """The shared (per-project) root a run's store dir belongs to — where the learnings KB
    and queue file live. In the dated layout that is the grandparent
    (``<project_root>/<date>/<run>`` → ``<project_root>``); for a legacy ``runs/<run>``
    store (or a flat per-run root) it stays the parent, exactly as before #523."""
    run_dir = Path(run_dir)
    if is_date_dirname(run_dir.parent.name):
        return run_dir.parent.parent
    return run_dir.parent


def iter_run_dirs(root: str | Path, *, max_depth: int = DEFAULT_WALK_DEPTH) -> Iterator[Path]:
    """Every run store dir beneath ``root``, sorted by path, at most ``max_depth`` levels
    down (``<project>/<date>/<run>`` is depth 3 below the top-level runs root; a legacy
    ``runs/<run>`` is depth 1). One walker serves the dashboard, panel-report, kb backfill
    and the CLI resolver, so they cannot disagree about what counts as a run. A run dir is
    yielded and NOT descended into (its ``stages/`` tree is not a run); hidden dirs are
    skipped; a missing root yields nothing."""
    root = Path(root)
    if max_depth < 1 or not root.is_dir():
        return

    def _walk(parent: Path, depth: int) -> Iterator[Path]:
        try:
            children = sorted(parent.iterdir())
        except OSError:
            return
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            if is_run_store_dir(child):
                yield child
            elif depth < max_depth:
                yield from _walk(child, depth + 1)

    yield from _walk(root, 1)


# --- opt-in migration of a legacy runs/ root --------------------------------------------


def _read_run_doc(run_dir: Path) -> dict | None:
    """The ``document_type == "run"`` doc in ``run_dir``, or None when none is readable."""
    import json

    for candidate in sorted(run_dir.glob("status-*.json")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("document_type") == "run":
            return data
    return None


def plan_migration(
    legacy_root: str | Path,
    dest_root: str | Path,
    *,
    project_name: str | None = None,
    name_for_ref: Callable[[str], str | None] | None = None,
) -> list[dict[str, str | None]]:
    """What ``orchestrator runs-migrate`` would do: one entry per run dir directly under
    ``legacy_root`` (the pre-#523 ``<project>/runs/<run>/`` shape), each either a ``move``
    to ``<dest_root>/<project>/<created date>/<run>/`` or a ``skip`` with its reason.

    The project name is ``project_name`` when given, else looked up from the run doc's
    ``project_ref`` through ``name_for_ref`` (the CLI passes the adapter loader); a run
    whose name cannot be determined, whose doc is unreadable or undated, or whose
    destination already exists is SKIPPED, never guessed or overwritten. Pure planning:
    nothing here moves a byte."""
    legacy_root = Path(legacy_root)
    dest_root = Path(dest_root)
    plan: list[dict[str, str | None]] = []
    if not legacy_root.is_dir():
        return plan
    for child in sorted(legacy_root.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or not is_run_store_dir(child):
            continue
        entry: dict[str, str | None] = {
            "run_id": child.name, "src": str(child), "dst": None, "action": "skip",
            "reason": None,
        }
        plan.append(entry)
        doc = _read_run_doc(child)
        if doc is None:
            entry["reason"] = "no readable run doc"
            continue
        run_id = str(doc.get("run_id") or child.name)
        entry["run_id"] = run_id
        created = str(doc.get("created_at") or "")
        if not is_date_dirname(created[:10]):
            entry["reason"] = "run doc has no created_at date"
            continue
        name = project_name
        ref = doc.get("project_ref")
        if name is None and ref and name_for_ref is not None:
            name = name_for_ref(str(ref))
        if not name:
            entry["reason"] = "project name unknown (run doc has no loadable project_ref; pass --project)"
            continue
        dst = run_dir_for(project_runs_root(dest_root, name), run_id, created)
        entry["dst"] = str(dst)
        if dst.exists():
            entry["reason"] = "destination already exists"
            continue
        entry["action"] = "move"
    return plan
