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
"""

from __future__ import annotations

import inspect

from orchestrator.engine import Engine
from orchestrator.schemas.status import Run


def test_every_create_run_setting_is_persisted_on_run() -> None:
    params = [p for p in inspect.signature(Engine.create_run).parameters if p != "self"]
    run_fields = set(Run.model_fields)
    missing = [p for p in params if p not in run_fields]
    assert not missing, (
        "Engine.create_run parameter(s) not persisted on the Run model: "
        f"{missing}. A run-level setting consulted after the per-command CLI process "
        "boundary must be stored on Run (see #206 / the persistence-audit design note)."
    )
