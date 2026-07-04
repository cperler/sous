"""Spec front door (Roadmap B / #18): idea → validated spec → dependency-ordered issues.

The missing upstream of a batch run. Today a run starts from an already-written GitHub
issue; nothing turns *an idea* into well-scoped, dependency-ordered issues that feed the
batch lane. This module is the deterministic half of that front door: it loads and
schema-validates a spec file (authored by the model during a conversation — see the
``spec-intake`` skill), validates the dependency DAG (duplicate ids, unknown refs,
self-edges, cycles → clear errors), topologically orders the tasks, renders a
human-readable filing plan, and files each task as an issue via a project's task source
in dependency order — translating local ids to the real issue refs of already-filed
tasks so ``Depends-on:`` lines point at live issues.

Pure and project-agnostic: it calls no model and knows nothing about a specific repo.
Issue creation goes through the task source's optional, duck-typed ``create_task`` hook
(the same pattern as the ``file_followup`` / ``publish_note`` evidence-out hooks in
``adapters/project/base.py``).
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from jsonschema import Draft202012Validator

from .cost_policy import estimate_to_usd
from .errors import OrchestratorError

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "spec.json"


class SpecError(OrchestratorError):
    """A spec file failed schema validation or DAG validation."""


@runtime_checkable
class IssueCreator(Protocol):
    """The one capability ``file_spec`` needs from a task source: create an issue and
    return its ref (e.g. ``#42``). Optional/duck-typed — a source without it can't file."""

    def create_task(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> str: ...


@functools.cache
def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _format_schema_errors(spec: Any) -> list[str]:
    validator = Draft202012Validator(_schema())
    msgs = []
    for err in sorted(validator.iter_errors(spec), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "<root>"
        msgs.append(f"{loc}: {err.message}")
    return msgs


def validate_schema(spec: Any) -> None:
    """Raise ``SpecError`` (listing every violation) unless ``spec`` matches spec.json."""
    errors = _format_schema_errors(spec)
    if errors:
        raise SpecError("spec failed schema validation:\n  - " + "\n  - ".join(errors))


def validate_dag(spec: dict) -> None:
    """Raise ``SpecError`` on a duplicate id, an unknown/self depends_on ref, or a cycle."""
    tasks = spec["tasks"]
    ids = [t["id"] for t in tasks]

    seen: set[str] = set()
    dups: set[str] = set()
    for i in ids:
        if i in seen:
            dups.add(i)
        seen.add(i)
    if dups:
        raise SpecError(f"duplicate task id(s): {', '.join(sorted(dups))}")

    idset = set(ids)
    for t in tasks:
        for dep in t.get("depends_on", []):
            if dep == t["id"]:
                raise SpecError(f"task {t['id']!r} depends on itself")
            if dep not in idset:
                raise SpecError(f"task {t['id']!r} depends on unknown task {dep!r}")

    topological_order(spec)  # raises SpecError on a cycle


def validate_spec(spec: Any) -> None:
    """Full validation: schema first (so DAG code can assume the shape), then the DAG."""
    validate_schema(spec)
    validate_dag(spec)


def load_spec(path: str | Path) -> dict:
    """Read, parse, and fully validate a spec file. Raises ``SpecError`` with a clear
    message on a missing file, malformed JSON, a schema violation, or a DAG error."""
    p = Path(path)
    if not p.exists():
        raise SpecError(f"spec file not found: {p}")
    try:
        spec = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecError(f"spec file is not valid JSON ({p}): {exc}") from exc
    validate_spec(spec)
    return spec


def topological_order(spec: dict) -> list[str]:
    """Local task ids in a dependency-respecting order (deps before dependents), stable
    by input order within each level. Raises ``SpecError`` if the graph has a cycle.

    Assumes ids are unique and refs resolve — call ``validate_dag`` first for clean
    errors; on its own this only detects the cycle."""
    tasks = spec["tasks"]
    ids = [t["id"] for t in tasks]
    deps = {t["id"]: list(t.get("depends_on", [])) for t in tasks}

    placed: list[str] = []
    placed_set: set[str] = set()
    remaining = list(ids)
    while remaining:
        ready = [i for i in remaining if all(d in placed_set for d in deps[i])]
        if not ready:
            raise SpecError(
                f"spec dependency graph contains a cycle among: {', '.join(remaining)}"
            )
        for i in ready:
            placed.append(i)
            placed_set.add(i)
            remaining.remove(i)
    return placed


def spec_slug(spec: dict) -> str:
    """The ``spec:<slug>`` batch label's slug — an explicit ``slug`` or one derived from
    the title, so every issue a spec files is queryable as one batch."""
    raw = spec.get("slug") or spec["title"]
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return slug or "spec"


def spec_label(spec: dict) -> str:
    return f"spec:{spec_slug(spec)}"


def _compose_body(body: str, dep_refs: list[str]) -> str:
    """The issue body plus a ``Depends-on:`` line naming the real refs of the tasks this
    one depends on (omitted when there are none). The batch lane reads structured
    depends_on; this line keeps the dependency human-visible on the issue itself."""
    if not dep_refs:
        return body
    return f"{body}\n\nDepends-on: {', '.join(dep_refs)}"


def plan(spec: dict) -> str:
    """A human-readable filing plan: the spec, its batch label, and each task in the
    exact order it would be filed, with its translated (local → placeholder) deps. No
    side effects — this is what ``spec plan`` prints and what ``spec file --dry-run``
    previews against."""
    order = topological_order(spec)
    by_id = {t["id"]: t for t in spec["tasks"]}
    label = spec_label(spec)

    lines = [
        f"Spec: {spec['title']}",
        f"Summary: {spec['summary']}",
        f"Batch label: {label}",
        f"Tasks: {len(order)} (filing order below)",
        "",
    ]
    for pos, local_id in enumerate(order, 1):
        t = by_id[local_id]
        labels = [*t.get("labels", []), label]
        lines.append(f"{pos}. [{local_id}] {t['title']}")
        deps = t.get("depends_on", [])
        if deps:
            lines.append(f"     depends-on (local): {', '.join(deps)}")
        lines.append(f"     labels: {', '.join(labels)}")
        extras = []
        if t.get("pipeline"):
            extras.append(f"pipeline={t['pipeline']}")
        if t.get("provider_tag"):
            extras.append(f"provider_tag={t['provider_tag']}")
        if t.get("estimate"):
            extras.append(f"estimate={t['estimate']}")
        if extras:
            lines.append(f"     {'  '.join(extras)}")
    return "\n".join(lines)


def estimate_budget(spec: dict, budget_usd: float | None = None) -> dict:
    """A-priori cost estimate for a spec's tasks (Roadmap-B / #18 bullet, now #34).

    Sums each task's ``estimate`` hint through the rough ``ESTIMATE_USD`` table (advisory
    math, NO model). Returns the per-task breakdown, the total, the count of tasks with no
    usable estimate (the total is a FLOOR when any are unestimated), and — when
    ``budget_usd`` is given — whether the estimate overruns it. Purely informational; the
    caller decides whether an overrun is a warning or (``--strict``) an error."""
    tasks = spec["tasks"]
    per_task: list[dict] = []
    total = 0.0
    unestimated = 0
    for t in tasks:
        usd = estimate_to_usd(t.get("estimate"))
        if usd is None:
            unestimated += 1
        else:
            total += usd
        per_task.append(
            {"id": t["id"], "title": t["title"],
             "estimate": t.get("estimate"), "estimate_usd": usd}
        )
    out: dict[str, Any] = {
        "total_estimate_usd": round(total, 4),
        "tasks": per_task,
        "unestimated": unestimated,
        "task_count": len(tasks),
    }
    if budget_usd is not None:
        out["budget_usd"] = budget_usd
        out["overrun"] = total > budget_usd
        out["remaining_usd"] = round(budget_usd - total, 4)
    return out


def render_estimate(est: dict) -> str:
    """Human-readable rendering of ``estimate_budget`` output for the ``spec plan`` CLI."""
    lines = [f"A-priori cost estimate ({est['task_count']} task(s), ROUGH — advisory only):"]
    for t in est["tasks"]:
        usd = t["estimate_usd"]
        shown = f"${usd:.2f}" if usd is not None else "unestimated"
        hint = t["estimate"] if t["estimate"] not in (None, "") else "—"
        lines.append(f"  - [{t['id']}] {t['title']}: {hint} -> {shown}")
    lines.append(f"Total estimated: ${est['total_estimate_usd']:.2f}")
    if est.get("unestimated"):
        lines.append(
            f"  (note: {est['unestimated']} task(s) had no usable estimate — the total is a FLOOR)"
        )
    if "budget_usd" in est:
        verdict = "OVER budget" if est["overrun"] else "within budget"
        lines.append(
            f"Budget: ${est['budget_usd']:.2f} — {verdict} "
            f"(${est['remaining_usd']:.2f} remaining)"
        )
    return "\n".join(lines)


def file_spec(spec: dict, task_source: Any, *, dry_run: bool = False) -> dict:
    """File each task as an issue via ``task_source`` in topological order.

    For each task, in dependency order: translate its local depends_on ids to the real
    refs of the already-filed tasks, append a ``Depends-on:`` line to the body, apply the
    task's labels plus the ``spec:<slug>`` batch label, and create the issue. Returns
    ``{spec_label, order, mapping (local id → issue ref), filed[...], dry_run}``.

    ``dry_run`` files nothing — it maps each local id to a ``(dry-run:<id>)`` placeholder
    so the caller sees exactly what would be created, including translated Depends-on
    lines. A real run needs the source to expose ``create_task`` (duck-typed)."""
    validate_spec(spec)
    order = topological_order(spec)
    by_id = {t["id"]: t for t in spec["tasks"]}
    label = spec_label(spec)

    create = getattr(task_source, "create_task", None)
    if not dry_run and not callable(create):
        raise SpecError(
            "task source cannot file issues: it exposes no create_task(title, body, "
            "labels) hook (use --dry-run to preview, or plug in a source that supports it)"
        )

    mapping: dict[str, str] = {}
    filed: list[dict] = []
    for local_id in order:
        t = by_id[local_id]
        dep_refs = [mapping[d] for d in t.get("depends_on", [])]
        body = _compose_body(t["body"], dep_refs)
        labels = [*t.get("labels", []), label]
        ref = f"(dry-run:{local_id})" if dry_run else create(t["title"], body, labels)
        mapping[local_id] = ref
        filed.append(
            {
                "local_id": local_id,
                "ref": ref,
                "title": t["title"],
                "labels": labels,
                "depends_on_refs": dep_refs,
            }
        )
    return {
        "spec_label": label,
        "order": order,
        "mapping": mapping,
        "filed": filed,
        "dry_run": dry_run,
    }


def archive_spec(spec: dict, result: dict, archive_dir: str | Path) -> Path:
    """Persist the just-filed spec so the conformance gate (#18 bullet 2) can find it.

    Writes ``<archive_dir>/<slug>.json`` — the validated spec verbatim plus a ``filed``
    block appended as provenance: the ``spec:<slug>`` batch label and the local-id →
    issue-ref mapping from ``result`` (a ``file_spec`` return). That mapping is what
    ``conformance_report`` walks to look up each spec task's real issue and its state,
    long after the run. Returns the path written.

    The caller skips this on ``--dry-run`` (nothing was filed, so there is no provenance
    to record). Overwrites a prior archive of the same slug — a re-file supersedes it."""
    d = Path(archive_dir)
    d.mkdir(parents=True, exist_ok=True)
    archived = dict(spec)
    archived["filed"] = {
        "spec_label": result["spec_label"],
        "mapping": result["mapping"],
    }
    path = d / f"{spec_slug(spec)}.json"
    path.write_text(json.dumps(archived, indent=2) + "\n", encoding="utf-8")
    return path
