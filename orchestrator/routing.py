"""Lane routing (target.md §4 — the orthogonal execution_mode × provider axes).

Generalizes the 3a/3b hardcoded interactive×claude into a per-stage lane decision.
Provider selection ports the as-built ADR-062 model: a global ``ORCHESTRATOR_PROVIDER``
switch, plus a per-task ``:codex`` tag that only applies to ``CODEX_ELIGIBLE_STAGES``
(the file-patching stages). Codex is always headless (it is never an in-session call),
so the two axes stay orthogonal with codex×interactive an honest empty cell.

The default Router reproduces 3a/3b exactly (everything → interactive×claude), so
existing behavior is preserved until a config flip selects another lane.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schemas.enums import ExecutionMode, Provider, Stage
from .schemas.status import Task
from .schemas.work import LanePolicy

# The file-patching stages a per-task :codex tag may route to codex (ports
# CODEX_ELIGIBLE_STAGES = implement-task-*/fix-* into the collapsed stage map).
DEFAULT_CODEX_ELIGIBLE: frozenset[Stage] = frozenset(
    {Stage.IMPLEMENT, Stage.SIMPLIFY, Stage.TEST}
)


@dataclass(frozen=True)
class Router:
    execution_mode: ExecutionMode = ExecutionMode.INTERACTIVE
    orchestrator_provider: Provider | None = None  # global switch (None => per-task/claude)
    codex_eligible_stages: frozenset[Stage] = field(default_factory=lambda: DEFAULT_CODEX_ELIGIBLE)
    allow_fallback: bool = True  # permit graceful model fallback (capacity downgrade + rate-limit)

    def _provider(self, stage: Stage, task: Task) -> Provider:
        # Cross-provider fallthrough (#7): a stage the engine already re-routed off codex (the
        # codex provider was out) stays on claude — one-way and never back to codex, so a
        # subsequent claude failure can't ping-pong. Checked FIRST so it overrides both the
        # global provider switch and a per-task :codex pin.
        if stage in task.fallthrough_stages:
            return Provider.CLAUDE
        if self.orchestrator_provider is Provider.CODEX:
            return Provider.CODEX
        if task.provider_tag == "codex" and stage in self.codex_eligible_stages:
            return Provider.CODEX
        return Provider.CLAUDE

    def lane_for(self, stage: Stage, task: Task) -> LanePolicy:
        provider = self._provider(stage, task)
        # Codex is never interactive — it runs as a subprocess (headless).
        mode = ExecutionMode.HEADLESS if provider is Provider.CODEX else self.execution_mode
        return LanePolicy(execution_mode=mode, provider=provider, allow_fallback=self.allow_fallback)


# 3a/3b default: everything on interactive×claude.
DEFAULT_ROUTER = Router()
