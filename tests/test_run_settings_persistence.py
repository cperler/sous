"""Process-boundary persistence guard for run-level settings (#206).

Every CLI subcommand rebuilds the Engine from constructor DEFAULTS — ``cli._engine``
passes only store/ledger/project/router/registry, never the tuning knobs. So a run-level
setting chosen at run-create time is only remembered inside the single process that set
it; a LATER subcommand (which is where filing/completion/retry decisions run) sees the
default unless the value was persisted on the Run document. #196 fixed the
``max_filed_followups`` instance of exactly this bug.

This test freezes the invariant that keeps the class of bug from recurring: every
parameter of ``Engine.create_run`` is a field on the ``Run`` model, so a new run-level
knob cannot be added to create_run without also being persisted. See
``docs/reviews/2026-07-18-run-level-settings-persistence-audit.md`` for the full audit.
``Engine.create_or_reuse_run`` (#280) is the second run-creating entry point and is held
to the same rule, so it cannot become a fresh unpersisted-settings hole.
"""

from __future__ import annotations

import inspect

import pytest

from orchestrator.engine import Engine
from orchestrator.schemas.status import Run


@pytest.mark.parametrize("entry_point", ["create_run", "create_or_reuse_run"])
def test_every_run_creation_setting_is_persisted_on_run(entry_point: str) -> None:
    fn = getattr(Engine, entry_point)
    params = [p for p in inspect.signature(fn).parameters if p != "self"]
    run_fields = set(Run.model_fields)
    missing = [p for p in params if p not in run_fields]
    assert not missing, (
        f"Engine.{entry_point} parameter(s) not persisted on the Run model: "
        f"{missing}. A run-level setting consulted after the per-command CLI process "
        "boundary must be stored on Run (see #206 / the persistence-audit design note)."
    )


def test_create_or_reuse_run_accepts_the_same_settings_as_create_run() -> None:
    """The idempotent entry point must not drift from ``create_run`` — a knob it cannot
    accept is a knob a create-or-reuse caller silently loses, and one it does not compare
    is a setting a reuse can silently disagree about (#280)."""
    create = inspect.signature(Engine.create_run).parameters
    reuse = inspect.signature(Engine.create_or_reuse_run).parameters
    assert list(create) == list(reuse)
    assert [(p.default, p.kind) for p in create.values()] == [
        (p.default, p.kind) for p in reuse.values()
    ]
    settings = [p for p in create if p not in ("self", "run_id")]
    assert list(Engine._REUSE_IMMUTABLE_SETTINGS) == settings
