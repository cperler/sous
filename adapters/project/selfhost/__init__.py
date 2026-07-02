"""Self-host project-config adapter (Phase 5 generality dogfood)."""

from __future__ import annotations

from .config import SelfHostConfig, get_config

# The ProjectConfig contract this adapter targets (kept in lockstep by this repo's suite).
CONTRACT_VERSION = 1

__all__ = ["SelfHostConfig", "get_config"]
