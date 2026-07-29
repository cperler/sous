"""Engine exceptions."""

from __future__ import annotations


class OrchestratorError(Exception):
    """Base for all engine errors."""


class StatusStoreError(OrchestratorError):
    """Status persistence/locking failures."""


class StatusNotFoundError(StatusStoreError):
    """A status document does not exist — the genuine "not found" case, distinct from an
    unreadable or corrupt-JSON file (which stay plain ``StatusStoreError``). A subclass so
    existing ``except StatusStoreError`` catches still cover it, but a caller probing for
    existence can catch ONLY this and let real I/O/parse failures bubble up (#112)."""


class RunExistsError(StatusStoreError):
    """A run document already exists for the requested run id (#280). Creation refuses
    rather than replacing it: an overwrite would orphan the run's task documents (they
    stay on disk but leave the new run's ``task_refs``) and erase its dependency graph,
    state and settings. Callers that genuinely want create-or-reuse call
    ``Engine.create_or_reuse_run``; everyone else picks a new run id."""


class ResumeError(OrchestratorError):
    """Invalid/incomplete state for resume."""


class CapacityExhausted(OrchestratorError):
    """No capacity-safe dispatch slot is available right now."""


class NoRunnerError(OrchestratorError):
    """No execution runner is registered for a required (mode, provider) cell."""


class ContractError(OrchestratorError):
    """A StageResult did not match the WorkItem it claims to answer."""
