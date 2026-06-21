"""DAG tests — esp. the transitive-cascade fix (D14)."""

from __future__ import annotations

import pytest

from orchestrator.dag import Dag, DagError
from orchestrator.schemas.enums import TaskState


def test_unknown_dep_rejected() -> None:
    with pytest.raises(DagError):
        Dag({"a": ["missing"]})


def test_cycle_rejected() -> None:
    with pytest.raises(DagError):
        Dag({"a": ["b"], "b": ["a"]})


def test_ready_and_unmet() -> None:
    dag = Dag({"a": [], "b": ["a"], "c": ["b"]})
    states = {"a": TaskState.PENDING, "b": TaskState.BLOCKED, "c": TaskState.BLOCKED}
    assert dag.ready_tasks(states) == ["a"]
    assert dag.unmet_deps("b", states) == ["a"]
    states["a"] = TaskState.COMPLETED
    assert dag.is_ready("b", states)
    assert dag.unlock_on_complete("a", states) == ["b"]


def test_transitive_cascade_blocks_grandchildren() -> None:
    # a -> b -> c -> d ; a fails. b, c, d must all cascade-block (the D14 fix:
    # the as-built blocked only b, letting c/d slip through).
    dag = Dag({"a": [], "b": ["a"], "c": ["b"], "d": ["c"]})
    states = {
        "a": TaskState.FAILED,
        "b": TaskState.BLOCKED,
        "c": TaskState.BLOCKED,
        "d": TaskState.BLOCKED,
    }
    assert dag.transitive_cascade("a", states) == {"b", "c", "d"}


def test_cascade_skips_terminal_and_diamond() -> None:
    # diamond: a -> {b, c} -> d. a fails. b,c,d block; an already-completed branch is skipped.
    dag = Dag({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]})
    states = {
        "a": TaskState.FAILED,
        "b": TaskState.COMPLETED,  # already done — not re-blocked
        "c": TaskState.RUNNING,
        "d": TaskState.BLOCKED,
    }
    assert dag.transitive_cascade("a", states) == {"c", "d"}


def test_direct_dependents_helper() -> None:
    dag = Dag({"a": [], "b": ["a"], "c": ["a"], "d": ["b"]})
    assert sorted(dag.dependents_of("a")) == ["b", "c"]
    assert dag.dependents_of("d") == []
