"""Single model + pricing config table (target.md §6.3, fixes as-built D3/D4).

The ONE place a model id or price is named. Stages reference models by *role*, not
by literal id, so a model bump is a one-line change here. Prices are current
(fixing the stale $15/$75 Opus and the `opus-4-7`/`sonnet-4-6` pins in the bash
system). Cost is computed here so the cost ledger has a single source of truth.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .schemas.work import TokenUsage


class ModelInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    # USD per million tokens (current values, not the stale bash pins).
    input_per_mtok: float
    output_per_mtok: float
    cache_read_mult: float = 0.10  # cache reads bill at 10% of input
    cache_write_mult: float = 1.25  # cache writes bill at 125% of input


class Role:
    """Stage role -> model. The engine asks for a role; the table resolves the id."""

    DEEP_REASON = "deep_reason"  # research/plan/implement/fix
    REVIEW = "review"  # evaluate/review/test/docs/pr — cheaper
    CHEAP_SHELL = "cheap_shell"  # setup/intake


# Current model pins (single source). Update here on a model bump.
_MODELS: dict[str, ModelInfo] = {
    "claude-opus-4-8": ModelInfo(id="claude-opus-4-8", input_per_mtok=5.0, output_per_mtok=25.0),
    "claude-sonnet-4-6": ModelInfo(id="claude-sonnet-4-6", input_per_mtok=3.0, output_per_mtok=15.0),
    "claude-haiku-4-5": ModelInfo(id="claude-haiku-4-5", input_per_mtok=1.0, output_per_mtok=5.0),
}

_ROLE_TO_MODEL: dict[str, str] = {
    Role.DEEP_REASON: "claude-opus-4-8",
    Role.REVIEW: "claude-sonnet-4-6",
    Role.CHEAP_SHELL: "claude-haiku-4-5",
}

# Rate-limit fallback chain (ports MODEL_CHAIN: opus -> sonnet -> haiku).
MODEL_CHAIN: tuple[str, ...] = ("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5")


class ModelTable:
    """Immutable view over the model/pricing config."""

    def model_for_role(self, role: str) -> str:
        return _ROLE_TO_MODEL[role]

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

    def fallback_after(self, model_id: str) -> str | None:
        """Next cheaper model in the chain, or None if at the end."""
        try:
            idx = MODEL_CHAIN.index(model_id)
        except ValueError:
            return None
        return MODEL_CHAIN[idx + 1] if idx + 1 < len(MODEL_CHAIN) else None


DEFAULT_MODEL_TABLE = ModelTable()
