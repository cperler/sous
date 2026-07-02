"""Hey Soo! reference project-config adapter."""

from __future__ import annotations

from .config import HeysooConfig, get_config

# The ProjectConfig contract this adapter targets (kept in lockstep by this repo's suite).
CONTRACT_VERSION = 1

__all__ = ["HeysooConfig", "get_config"]
