"""Deterministic orchestration engine (Phase 3a).

The engine is pure Python + a CLI. It never calls a model: it emits WorkItems and
consumes StageResults (the §4 seam). Model calls happen only in execution-adapter
runners.
"""

from __future__ import annotations

__version__ = "0.1.0"
