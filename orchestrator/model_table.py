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
    # Mythos-tier, above Opus — reachable ONLY via an explicit per-task pin (#84), never a
    # role default. Price is the published Anthropic API rate ($10/$50 per Mtok, 2x Opus 5),
    # source: platform.claude.com/docs/en/about-claude/models/overview (Claude Fable 5 row,
    # re-confirmed 2026-07-25 with the Opus 5 / Sonnet 5 bump).
    "claude-fable-5": ModelInfo(id="claude-fable-5", input_per_mtok=10.0, output_per_mtok=50.0),
    "claude-opus-5": ModelInfo(id="claude-opus-5", input_per_mtok=5.0, output_per_mtok=25.0),
    # Sonnet 5 carries an INTRODUCTORY rate of $2/$10 per Mtok through 2026-08-31, after
    # which it reverts to the $3/$15 pinned here. We deliberately price at the standard
    # rate rather than the discount: the ledger feeds the `--budget-usd` hard-PAUSE gate,
    # so over-estimating spend fails safe (an early pause), while pricing the discount
    # would silently under-report every row once the intro window closes. Revisit after
    # 2026-08-31 only to confirm — no edit should be needed.
    "claude-sonnet-5": ModelInfo(id="claude-sonnet-5", input_per_mtok=3.0, output_per_mtok=15.0),
    "claude-haiku-4-5": ModelInfo(id="claude-haiku-4-5", input_per_mtok=1.0, output_per_mtok=5.0),
    # Superseded claude tiers — retained for PRICING historical ledger rows (runs dispatched
    # before the Opus 5 / Sonnet 5 bump), not for dispatch. Removing them would make every
    # prior row unpriced (`priced=False`) and corrupt historical cost reports.
    "claude-opus-4-8": ModelInfo(id="claude-opus-4-8", input_per_mtok=5.0, output_per_mtok=25.0),
    "claude-sonnet-4-6": ModelInfo(id="claude-sonnet-4-6", input_per_mtok=3.0, output_per_mtok=15.0),
    # codex (OpenAI) — the ids passed to `codex exec -m`. Re-probed live 2026-07-31 against
    # codex-cli 0.145.0 on this ChatGPT-plan account: the SIX ids below marked "accepted" all
    # dispatch, and gpt-5-codex/gpt-5/gpt-5.5-mini still 400 "not supported when using Codex
    # with a ChatGPT account" — same rejection set as the 2026-07-04 probe, but the plan now
    # exposes a real TIER LADDER where it previously exposed only gpt-5.5, so the role map and
    # fallback chain below are no longer degenerate.
    # Prices are the 2026-07-30 rates: Terra was cut 20% and Luna 80% from the June launch
    # ($2.50/$15 and $1.00/$6); Sol was not cut. Sol "Fast" mode bills at 2x Standard and is
    # NOT modelled here — nothing in this engine requests it.
    "gpt-5.6-sol": ModelInfo(id="gpt-5.6-sol", input_per_mtok=5.0, output_per_mtok=30.0),
    "gpt-5.6-terra": ModelInfo(id="gpt-5.6-terra", input_per_mtok=2.0, output_per_mtok=12.0),
    "gpt-5.6-luna": ModelInfo(id="gpt-5.6-luna", input_per_mtok=0.20, output_per_mtok=1.20),
    "gpt-5.5": ModelInfo(id="gpt-5.5", input_per_mtok=1.25, output_per_mtok=10.0),
    "gpt-5.4": ModelInfo(id="gpt-5.4", input_per_mtok=2.5, output_per_mtok=15.0),
    "gpt-5.4-mini": ModelInfo(id="gpt-5.4-mini", input_per_mtok=0.75, output_per_mtok=4.5),
    # Rejected by this plan (400) — retained ONLY so historical ledger rows dispatched when
    # they worked still price cleanly. Never dispatch these.
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
        Role.DEEP_REASON: "claude-opus-5",
        Role.REVIEW: "claude-sonnet-5",
        Role.CHEAP_SHELL: "claude-haiku-4-5",
    },
    # The 5.6 ladder maps 1:1 onto the three roles, mirroring the claude tiering: sol is the
    # frontier agentic-coding tier, terra the balanced everyday tier, luna the fast/cheap one.
    # This replaces the degenerate all-gpt-5.5 map that existed only because the plan used to
    # expose a single tier (#84).
    Provider.CODEX: {
        Role.DEEP_REASON: "gpt-5.6-sol",
        Role.REVIEW: "gpt-5.6-terra",
        Role.CHEAP_SHELL: "gpt-5.6-luna",
    },
}

# Rate-limit fallback chain, per provider (ports MODEL_CHAIN: opus -> sonnet -> haiku).
# Within a provider a rate-limited model degrades down its OWN chain (fallback_after). The
# cross-provider fallthrough (codex -> claude, #7) lives in the engine, not this table: it is
# a LANE swap once the same-provider chain is exhausted, not another entry in the chain.
_MODEL_CHAINS: dict[Provider, tuple[str, ...]] = {
    # fable sits at the HEAD (above opus) so a rate-limited fable pin degrades to opus
    # naturally (fallback_after('claude-fable-5') == 'claude-opus-5'). Nothing dispatches
    # chain[0] by default — the role defaults below stay opus/sonnet/haiku, and both the
    # capacity downgrade and rate-limit fallback only walk DOWN the chain, never up into
    # fable — so fable is reachable only through the per-task model pin (#84).
    # Superseded tiers (opus-4-8, sonnet-4-6) are deliberately NOT in the chain: they stay
    # priceable in _MODELS for historical rows, but a rate-limited Opus 5 degrades straight
    # to Sonnet 5 rather than sideways into a previous generation.
    Provider.CLAUDE: ("claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"),
    # A REAL descending chain now that the plan exposes tiers: a rate-limited sol degrades to
    # terra, terra to luna, and only luna (the floor) goes to cooldown or the #7 cross-provider
    # fallthrough. gpt-5.5/5.4/5.4-mini are deliberately NOT in the chain — like the superseded
    # claude tiers they stay priceable and pinnable, but a rate-limited 5.6 degrades down its
    # own generation rather than sideways into a previous one.
    Provider.CODEX: ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
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


# Friendly aliases for the per-task model pin (#84): the human types `--model fable`, not the
# full table id. The codex 5.6 tiers get short names for the same reason the claude ones do —
# they are now a real ladder, not the single tier that made an alias pointless. Older codex ids
# (gpt-5.5/5.4/5.4-mini) stay alias-free and pass through by their exact id.
_MODEL_ALIASES: dict[str, str] = {
    "fable": "claude-fable-5",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
    "sol": "gpt-5.6-sol",
    "terra": "gpt-5.6-terra",
    "luna": "gpt-5.6-luna",
}


def provider_for_model(model_id: str) -> Provider:
    """The provider a resolved model id belongs to — the seam add_task uses to validate a
    per-task pin against the task's provider tag (a codex-tagged task can't pin a claude id
    and vice versa, #84). Claude ids are `claude-*`, codex ids are `gpt-*`, ENGINE is NONE."""
    if model_id.startswith("claude-"):
        return Provider.CLAUDE
    if model_id.startswith("gpt-"):
        return Provider.CODEX
    if model_id == ENGINE_MODEL:
        return Provider.NONE
    raise ValueError(f"cannot classify provider for model id {model_id!r}")


def resolve_model_alias(name: str) -> str:
    """Resolve a friendly alias (`fable`/`opus`/`sonnet`/`haiku`) OR an exact table id (incl.
    `gpt-5.5`) to a canonical model id. Unknown names raise a ValueError listing every valid
    name — the single place `--model` input is normalized before it lands on a Task pin."""
    if name in _MODEL_ALIASES:
        return _MODEL_ALIASES[name]
    if name in _MODELS and name != ENGINE_MODEL:
        return name
    valid = sorted(set(_MODEL_ALIASES) | (set(_MODELS) - {ENGINE_MODEL}))
    raise ValueError(f"unknown model {name!r}; valid names: {', '.join(valid)}")


DEFAULT_MODEL_TABLE = ModelTable()
