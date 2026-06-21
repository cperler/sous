"""Engine exceptions."""

from __future__ import annotations


class OrchestratorError(Exception):
    """Base for all engine errors."""


class StatusStoreError(OrchestratorError):
    """Status persistence/locking failures."""


class ResumeError(OrchestratorError):
    """Invalid/incomplete state for resume."""


class CapacityExhausted(OrchestratorError):
    """No capacity-safe dispatch slot is available right now."""


class NoRunnerError(OrchestratorError):
    """No execution runner is registered for a required (mode, provider) cell."""


class ContractError(OrchestratorError):
    """A StageResult did not match the WorkItem it claims to answer."""
