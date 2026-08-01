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


class SchemaVersionError(StatusStoreError):
    """A status document's ``schema_version`` is one this engine cannot safely read (#275).

    Raised for a FUTURE version (written by a newer engine) or an unparseable one — never
    for a version on the migration ladder, which loads normally. A subclass of
    ``StatusStoreError`` so existing ``except StatusStoreError`` call sites still cover it,
    and deliberately NOT a subclass of ``StatusNotFoundError``: the document exists and is
    valid JSON, so probing for existence must not mistake "too new to read" for "absent"
    and go on to create a fresh doc over it.

    The refusal happens at READ, before any mutation, which is what makes it fail CLOSED: a
    doc this engine cannot fully understand is never loaded, so it is never re-serialized
    from a lossy in-memory model and written back with its unknown fields dropped.
    """


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


class SupervisorParkDeferred(OrchestratorError):
    """Signal that a low-context park must wait for existing leases to drain.

    ``in_flight`` identifies the leases that keep the run away from a safe stage
    boundary; ``projection`` preserves the failed context calculation for reporting.
    The interactive driver should stop refilling work, record those results, then retry
    the guarded dispatch so it can park without stranding a lease.
    """

    def __init__(self, in_flight: list[str], projection: dict) -> None:
        self.in_flight = in_flight
        self.projection = projection
        super().__init__(
            "supervisor context is below the dispatch threshold; stop refilling and "
            f"record the in-flight task(s) before parking: {in_flight}"
        )


class NoRunnerError(OrchestratorError):
    """No execution runner is registered for a required (mode, provider) cell."""


class ContractError(OrchestratorError):
    """A StageResult did not match the WorkItem it claims to answer."""
