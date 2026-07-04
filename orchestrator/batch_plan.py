"""Batch plan (#57): auto-analysis producer over an ALREADY-FILED batch of issues.

The other producer of the DAG the scheduler consumes. The spec front door (#18,
``spec_intake``) covers batches that ORIGINATE from an idea — it authors the issues and
their edges together. This module covers the opposite case: a pile of issues that were
filed independently (no shared author, no encoded edges) that a human wants to run as one
batch. The dependency analysis — which issue depends on which, which lane fits each — is
model work (it reads prose issue bodies), so it lives in the ``batch-plan`` skill; this
module is the deterministic half around it: it loads and schema-validates the model's
proposed plan, validates the dependency DAG (dups, unknown refs, self-edges, cycles),
topologically orders it, and applies it to a run by calling ``Engine.add_task`` per entry.

Pure and project-agnostic: it calls no model. The DAG checks REUSE ``spec_intake`` (cycle
detection, topological order) rather than forking a second graph validator — the only new
rule is that an edge may also point at an already-terminal *known* issue id (a real issue
outside this plan), which imposes no scheduling constraint and so is dropped from the graph
handed to ``add_task`` (the engine's ``Dag`` requires every edge to resolve to a task in the
run — an external already-done dependency would otherwise break dispatch).
"""

from __future__ import annotations

import functools
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from . import spec_intake
from .errors import OrchestratorError
from .schemas.enums import ExecutionLane, Stage

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "batch_plan.json"


class BatchPlanError(OrchestratorError):
    """A batch plan failed schema validation or DAG validation."""


@functools.cache
def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _format_schema_errors(plan: Any) -> list[str]:
    validator = Draft202012Validator(_schema())
    msgs = []
    for err in sorted(validator.iter_errors(plan), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "<root>"
        msgs.append(f"{loc}: {err.message}")
    return msgs


def validate_schema(plan: Any) -> None:
    """Raise ``BatchPlanError`` (listing every violation) unless ``plan`` matches
    batch_plan.json."""
    errors = _format_schema_errors(plan)
    if errors:
        raise BatchPlanError(
            "batch plan failed schema validation:\n  - " + "\n  - ".join(errors)
        )


def _internal_spec(plan: dict, plan_ids: set[str]) -> dict:
    """A ``spec_intake``-shaped view of the plan for cycle detection / topo order: each
    task's ``id`` is its ``task_id`` and its ``depends_on`` keeps ONLY plan-internal edges
    (edges to external known ids don't gate scheduling and must not enter the graph). This
    is how the DAG checks are reused instead of forked."""
    return {
        "tasks": [
            {
                "id": t["task_id"],
                "depends_on": [d for d in t.get("depends_on", []) if d in plan_ids],
            }
            for t in plan["tasks"]
        ]
    }


def validate_plan(plan: dict, known_ids: Iterable[str] | None = None) -> None:
    """Validate the plan's DAG. Raises ``BatchPlanError`` on a duplicate ``task_id``, a
    self-edge, a cycle, or an edge that resolves to neither a task in the plan nor a
    ``known_ids`` member.

    ``known_ids`` is the set of real, existing task ids an edge may reference outside the
    plan (typically the open-issue ids the ``candidates`` fetch returned, which include
    already-terminal ones). Pass ``None`` to skip external-ref verification entirely — an
    offline ``validate`` with no task source can only vouch for a self-contained plan, so
    it permits (unverified) external edges while still enforcing every internal check.

    The dup/self/cycle checks are ``spec_intake``'s, run over an internal-edges-only view
    of the plan (see ``_internal_spec``); only the external-ref rule is new here."""
    validate_schema(plan)
    tasks = plan["tasks"]
    plan_ids = {t["task_id"] for t in tasks}

    if known_ids is not None:
        valid_targets = plan_ids | set(known_ids)
        for t in tasks:
            for dep in t.get("depends_on", []):
                if dep == t["task_id"]:
                    continue  # a self-edge is spec_intake's error to report, below
                if dep not in valid_targets:
                    raise BatchPlanError(
                        f"task {t['task_id']!r} depends on {dep!r}, which is neither a "
                        "task in the plan nor a known issue id"
                    )

    # Reuse spec_intake's dup + self-edge + cycle detection over the internal view.
    try:
        spec_intake.validate_dag(_internal_spec(plan, plan_ids))
    except spec_intake.SpecError as exc:
        raise BatchPlanError(str(exc)) from exc


def load_plan(path: str | Path) -> dict:
    """Read, parse, and schema-validate a batch plan file. Raises ``BatchPlanError`` with
    a clear message on a missing file, malformed JSON, or a schema violation. (DAG
    validation needs the known-id set, so it's a separate ``validate_plan`` call.)"""
    p = Path(path)
    if not p.exists():
        raise BatchPlanError(f"batch plan file not found: {p}")
    try:
        plan = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BatchPlanError(f"batch plan file is not valid JSON ({p}): {exc}") from exc
    validate_schema(plan)
    return plan


def topological_order(plan: dict) -> list[str]:
    """The plan's ``task_id``s in dependency-respecting order (internal edges only, stable
    by input order). Assumes the plan already validated — call ``validate_plan`` first."""
    plan_ids = {t["task_id"] for t in plan["tasks"]}
    return spec_intake.topological_order(_internal_spec(plan, plan_ids))


def _lane(pipeline: str | None) -> ExecutionLane | None:
    """Translate a plan ``pipeline`` hint (full/lite/micro or null) into the run lane whose
    preset ``add_task`` resolves. A lane IS a named pipeline (design pass §1), so this is the
    natural encoding — null leaves ``add_task`` to inherit the run's default lane."""
    return ExecutionLane(pipeline) if pipeline else None


def apply_plan(
    engine: Any,
    run_id: str,
    plan: dict,
    *,
    known_ids: Iterable[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Add each plan task to ``run_id`` via ``engine.add_task``, in topological order.

    Validates first (``validate_plan``). For each task, in dependency order: pass its
    plan-internal ``depends_on`` edges (external already-terminal ids are dropped — they
    impose no scheduling constraint and the engine's ``Dag`` rejects edges to non-tasks),
    its lane preset (from the ``pipeline`` hint), its ``provider_tag`` and
    ``deterministic_stages``. Emits ONE ``batch_planned`` event summarizing what was added.

    ``dry_run`` adds nothing and emits no event — it returns exactly what WOULD be added so
    the caller (and the human) can inspect the plan before committing to it. Returns
    ``{run_id, order, added[...], dropped_external_deps{...}, dry_run}``."""
    validate_plan(plan, known_ids)
    by_id = {t["task_id"]: t for t in plan["tasks"]}
    plan_ids = set(by_id)
    order = topological_order(plan)

    added: list[dict] = []
    dropped: dict[str, list[str]] = {}
    for tid in order:
        t = by_id[tid]
        all_deps = list(t.get("depends_on", []))
        internal_deps = [d for d in all_deps if d in plan_ids]
        external = [d for d in all_deps if d not in plan_ids]
        if external:
            dropped[tid] = external
        det = [Stage(s) for s in (t.get("deterministic_stages") or [])] or None
        entry = {
            "task_id": tid,
            "depends_on": internal_deps,
            "pipeline": t.get("pipeline"),
            "provider_tag": t.get("provider_tag"),
            "deterministic_stages": [s.value for s in (det or ())],
            "rationale": t.get("rationale", ""),
        }
        if not dry_run:
            engine.add_task(
                run_id,
                tid,
                lane=_lane(t.get("pipeline")),
                depends_on=internal_deps,
                provider_tag=t.get("provider_tag"),
                deterministic_stages=det,
            )
        added.append(entry)

    if not dry_run:
        engine.store.append_event(
            run_id,
            {
                "ts": datetime.now(UTC).isoformat(),
                "type": "batch_planned",
                "run_id": run_id,
                "count": len(added),
                "order": order,
                "tasks": added,
                "dropped_external_deps": dropped,
            },
        )

    return {
        "run_id": run_id,
        "order": order,
        "added": added,
        "dropped_external_deps": dropped,
        "dry_run": dry_run,
    }
