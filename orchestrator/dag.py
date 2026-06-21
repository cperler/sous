"""Dependency DAG (target.md §3).

Fix-forward D14: cascade-blocking is **transitive** here — when a task fails, every
transitive dependent is cascade-blocked at failure time, not just direct dependents
(the as-built bug let grandchildren slip through to a terminal sweep).
"""

from __future__ import annotations

from collections import deque

from .errors import OrchestratorError
from .schemas.enums import TERMINAL_TASK_STATES, TaskState


class DagError(OrchestratorError):
    """Malformed dependency graph (e.g. a cycle or an unknown dependency)."""


class Dag:
    """A dependency graph over task ids with state-aware queries.

    ``graph`` maps task_id -> list of task_ids it depends on. All referenced ids
    must be present as keys.
    """

    def __init__(self, graph: dict[str, list[str]]) -> None:
        self._deps: dict[str, list[str]] = {k: list(v) for k, v in graph.items()}
        self._check_known()
        # Reverse edges: task_id -> tasks that depend on it (its dependents).
        self._dependents: dict[str, list[str]] = {k: [] for k in self._deps}
        for task, deps in self._deps.items():
            for dep in deps:
                self._dependents[dep].append(task)
        self._assert_acyclic()

    def _check_known(self) -> None:
        for task, deps in self._deps.items():
            for dep in deps:
                if dep not in self._deps:
                    raise DagError(f"task {task!r} depends on unknown task {dep!r}")

    def _assert_acyclic(self) -> None:
        # Kahn's algorithm; if not all nodes drain, there is a cycle.
        indeg = {k: len(v) for k, v in self._deps.items()}
        q = deque(k for k, d in indeg.items() if d == 0)
        seen = 0
        while q:
            node = q.popleft()
            seen += 1
            for dep in self._dependents[node]:
                indeg[dep] -= 1
                if indeg[dep] == 0:
                    q.append(dep)
        if seen != len(self._deps):
            raise DagError("dependency graph contains a cycle")

    def deps_of(self, task_id: str) -> list[str]:
        return list(self._deps[task_id])

    def dependents_of(self, task_id: str) -> list[str]:
        """Direct dependents only."""
        return list(self._dependents[task_id])

    def unmet_deps(self, task_id: str, states: dict[str, TaskState]) -> list[str]:
        """Deps that are not yet COMPLETED."""
        return [d for d in self._deps[task_id] if states.get(d) is not TaskState.COMPLETED]

    def is_ready(self, task_id: str, states: dict[str, TaskState]) -> bool:
        """A non-started task whose every dependency is COMPLETED."""
        if states.get(task_id) not in (TaskState.PENDING, TaskState.BLOCKED, None):
            return False
        return not self.unmet_deps(task_id, states)

    def ready_tasks(self, states: dict[str, TaskState]) -> list[str]:
        return [t for t in self._deps if self.is_ready(t, states)]

    def unlock_on_complete(
        self, completed_id: str, states: dict[str, TaskState]
    ) -> list[str]:
        """Direct dependents of ``completed_id`` that are now ready."""
        after = dict(states)
        after[completed_id] = TaskState.COMPLETED
        return [d for d in self._dependents[completed_id] if self.is_ready(d, after)]

    def transitive_cascade(
        self, failed_id: str, states: dict[str, TaskState]
    ) -> set[str]:
        """All TRANSITIVE dependents of a failed task to cascade-block (fix D14).

        BFS over the reverse edges. Already-terminal tasks (completed/failed/
        cascade_blocked) are not re-blocked. Returns the set of task ids that
        should transition to ``cascade_blocked``.
        """

        blocked: set[str] = set()
        q = deque(self._dependents[failed_id])
        while q:
            node = q.popleft()
            if node in blocked:
                continue
            if states.get(node) in TERMINAL_TASK_STATES:
                continue
            blocked.add(node)
            q.extend(self._dependents[node])
        return blocked
