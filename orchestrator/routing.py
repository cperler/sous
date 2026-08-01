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


def engine_lane_required(stage: Stage, lane: LanePolicy) -> str | None:
    """Reason ``stage`` CANNOT run on ``lane`` and must fall back to the deterministic ENGINE
    lane — or ``None`` when the model lane is fine (#364).

    This is a lane CAPABILITY veto, not a preference: unlike ``deterministic_stages`` (a
    per-task choice about where the cheap mechanical stages should run), the caller has no
    option to decline it, because the model lane physically cannot do the work.

    The one case today is DELIVER on codex. DELIVER pushes the task branch and then opens the
    PR, and codex's sandbox breaks the push: ``git-credential-osxkeychain`` cannot reach the
    keychain from inside it, so git falls through to a credential path that raises a **GUI
    passkey dialog**. Confirmed by a ``git push --dry-run`` inside a real ``codex exec``
    sandbox — it succeeded only because a human was at the machine to click the prompt. A
    headless batch is by definition unattended (often detached via ``setsid``), so there the
    push blocks until the dispatch timeout, and the retry blocks identically. That is the
    silent-stall shape #351 already fought once, arriving through a different door.

    Fixing it by widening the sandbox is not available: #351 granted network egress, which is
    what let the handshake get far enough to prompt at all. The credential path itself is
    interactive, and overriding ``credential.helper`` does not suppress ``osxkeychain``.

    So DELIVER goes to the ENGINE lane, where ``DeterministicDeliverRunner`` pushes and opens
    the PR from the engine's own process — outside any sandbox, where the keychain answers
    without a dialog — at \\$0 and with no model call. That is the same argument the
    mechanical stages already make generally: don't ask a model to run ``gh pr
    create``. The cost is the model DELIVER's docstring-refresh pass, which the deterministic
    runner documents itself as not doing.

    NOT expressed as a router provider swap (codex DELIVER -> claude DELIVER): that would keep
    a model on a mechanical stage, bill it, and quietly make an "all-codex" run not all-codex.
    """
    if stage is Stage.DELIVER and lane.provider is Provider.CODEX:
        return "codex_sandbox_cannot_push"
    return None


# 3a/3b default: everything on interactive×claude.
DEFAULT_ROUTER = Router()
