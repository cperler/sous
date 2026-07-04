"""Single model + pricing config table (target.md §6.3, fixes as-built D3/D4).

The ONE place a model id or price is named. Stages reference models by *role*, not
by literal id, so a model bump is a one-line change here. Prices are current
(fixing the stale $15/$75 Opus and the `opus-4-7`/`sonnet-4-6` pins in the bash
system). Cost is computed here so the cost ledger has a single source of truth.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from .schemas.enums import Provider
from .schemas.work import TokenUsage

_log = logging.getLogger(__name__)


class ModelInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    # USD per million tokens (current values, not the stale bash pins).
    input_per_mtok: float
    output_per_mtok: float
    cache_read_mult: float = 0.10  # cache reads bill at 10% of input
    cache_write_mult: float = 1.25  # cache writes bill at 125% of input


# The sentinel model id for a deterministic (non-model) ENGINE-lane stage.
ENGINE_MODEL = "engine"


class Role:
    """Stage role -> model. The engine asks for a role; the table resolves the id."""

    DEEP_REASON = "deep_reason"  # research/plan/implement/fix
    REVIEW = "review"  # evaluate/review/test/docs/pr — cheaper
    CHEAP_SHELL = "cheap_shell"  # setup/intake


# Current model pins (single source). Update here on a model bump. Keyed by id and
# spanning BOTH providers, so a codex-routed stage is priced from the codex prices
# rather than a claude row (roadmap E1). Codex/OpenAI prices are current pins — verify
# on a provider price change the same as the claude rows.
_MODELS: dict[str, ModelInfo] = {
    # claude
    "claude-opus-4-8": ModelInfo(id="claude-opus-4-8", input_per_mtok=5.0, output_per_mtok=25.0),
    "claude-sonnet-4-6": ModelInfo(id="claude-sonnet-4-6", input_per_mtok=3.0, output_per_mtok=15.0),
    "claude-haiku-4-5": ModelInfo(id="claude-haiku-4-5", input_per_mtok=1.0, output_per_mtok=5.0),
    # codex (OpenAI) — the ids passed to `codex exec -m`.
    "gpt-5-codex": ModelInfo(id="gpt-5-codex", input_per_mtok=1.25, output_per_mtok=10.0),
    "gpt-5": ModelInfo(id="gpt-5", input_per_mtok=1.25, output_per_mtok=10.0),
    "gpt-5-mini": ModelInfo(id="gpt-5-mini", input_per_mtok=0.25, output_per_mtok=2.0),
    # sentinel for the deterministic ENGINE lane: no model call, always $0. Present so
    # its ledger row prices cleanly (priced=True, cost 0) rather than warning as unpriced.
    ENGINE_MODEL: ModelInfo(id="engine", input_per_mtok=0.0, output_per_mtok=0.0),
}

# Role -> model id, keyed by provider so `model_for_role` is provider-aware: a
# codex-routed stage resolves to a codex id, not a claude one shelled to `codex exec -m`.
_ROLE_TO_MODEL: dict[Provider, dict[str, str]] = {
    Provider.CLAUDE: {
        Role.DEEP_REASON: "claude-opus-4-8",
        Role.REVIEW: "claude-sonnet-4-6",
        Role.CHEAP_SHELL: "claude-haiku-4-5",
    },
    Provider.CODEX: {
        Role.DEEP_REASON: "gpt-5-codex",
        Role.REVIEW: "gpt-5",
        Role.CHEAP_SHELL: "gpt-5-mini",
    },
}

# Rate-limit fallback chain, per provider (ports MODEL_CHAIN: opus -> sonnet -> haiku).
# Within a provider a rate-limited model degrades down its OWN chain (fallback_after). The
# cross-provider fallthrough (codex -> claude, #7) lives in the engine, not this table: it is
# a LANE swap once the same-provider chain is exhausted, not another entry in the chain.
_MODEL_CHAINS: dict[Provider, tuple[str, ...]] = {
    Provider.CLAUDE: ("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"),
    Provider.CODEX: ("gpt-5-codex", "gpt-5", "gpt-5-mini"),
}

# Back-compat alias: the claude chain (the default lane). fallback_after searches all
# chains, so callers need not know the provider.
MODEL_CHAIN: tuple[str, ...] = _MODEL_CHAINS[Provider.CLAUDE]


class ModelTable:
    """Immutable view over the model/pricing config."""

    def model_for_role(self, role: str, provider: Provider = Provider.CLAUDE) -> str:
        return _ROLE_TO_MODEL[provider][role]

    def info(self, model_id: str) -> ModelInfo:
        if model_id not in _MODELS:
            raise KeyError(f"unknown model id: {model_id!r} (add it to model_table)")
        return _MODELS[model_id]

    def cost_usd(self, model_id: str, usage: TokenUsage) -> float:
        """Compute cost from token usage using the single price table."""
        m = self.info(model_id)
        cost = (
            usage.input * m.input_per_mtok
            + usage.output * m.output_per_mtok
            + usage.cache_read * m.input_per_mtok * m.cache_read_mult
            + usage.cache_write * m.input_per_mtok * m.cache_write_mult
        ) / 1_000_000
        return round(cost, 6)

    def try_cost_usd(self, model_id: str, usage: TokenUsage) -> tuple[float, bool]:
        """Tolerant pricing: (cost, priced). An unknown model id logs a warning and
        prices at 0.0 with ``priced=False`` instead of raising — the ledger must still
        record the call (mirrors ``analysis()``'s tolerance). The row's ``priced`` flag
        surfaces the gap; the call is never silently dropped."""
        try:
            return self.cost_usd(model_id, usage), True
        except KeyError:
            _log.warning("unpriced model id %r — recording call at 0.0 cost", model_id)
            return 0.0, False

    def fallback_after(self, model_id: str) -> str | None:
        """Next cheaper model in the same provider's chain, or None if at the end /
        not in any chain."""
        for chain in _MODEL_CHAINS.values():
            try:
                idx = chain.index(model_id)
            except ValueError:
                continue
            return chain[idx + 1] if idx + 1 < len(chain) else None
        return None


DEFAULT_MODEL_TABLE = ModelTable()
