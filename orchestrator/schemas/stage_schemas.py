"""Canonical per-stage output schemas (target.md §4/§7; closes the codex-validation gap).

The 6 collapsed stages each have a JSON Schema describing the structured output a
runner must return (the ``Return:`` contract in ``stages.py``). These are **universal
engine contracts**, not project-specific, so they live with the engine and every
project-config adapter can expose them via ``schema_for`` for free — giving the codex
runner real full-validation instead of a schema injected only in tests.

A project may override a stage's schema by dropping a ``<ref>.json`` into a local
schemas directory (seeded by the scaffold); ``resolve_stage_schema`` prefers that.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

_CANONICAL_DIR = Path(__file__).parent / "stages"


@functools.cache
def load_stage_schema(ref: str) -> dict | None:
    """The engine's canonical JSON Schema for a stage ``schema_ref`` (or None if unknown)."""
    path = _CANONICAL_DIR / f"{ref}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_stage_schema(ref: str, *, local_dir: Path | str | None = None) -> dict | None:
    """schema_for resolution: a project-local override (``local_dir/<ref>.json``) wins,
    else the engine canonical. Adapters delegate their ``schema_for`` here."""
    if local_dir is not None:
        local = Path(local_dir) / f"{ref}.json"
        if local.exists():
            return json.loads(local.read_text(encoding="utf-8"))
    return load_stage_schema(ref)
