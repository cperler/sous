"""Property-based (Hypothesis) coverage for the context-ceiling invariants (#38).

The hand-written tests in ``test_context_plane.py`` nail specific byte-counted scenarios;
these stress the sizing math and tie-break logic across randomly generated mixes of stage
outputs, asserting the invariants that must hold for EVERY input rather than a few crafted
ones. They run over the post-#26 implementation (weights computed once, eviction order
settled up front) and lock its observable contract.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from orchestrator.schemas.enums import STAGE_ORDER, Stage
from orchestrator.schemas.status import Task
from orchestrator.state_machine import (
    _MAX_CONTEXT_BYTES,
    CONTEXT_KEYS,
    _absorb_outputs,
    _context_bytes,
    _enforce_context_ceiling,
)
from tests.test_context_plane import make_result_stub

# Keep the suite fast: modest example counts, and no per-example deadline (a large-context
# json.dumps can momentarily blow a tight deadline on a busy machine → spurious flakes).
_FAST = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# Every whitelisted context key and, for the tie-break invariant, its pipeline position.
_ALL_KEYS: list[str] = [key for stage in STAGE_ORDER for key in CONTEXT_KEYS[stage]]
_KEY_STAGE_INDEX: dict[str, int] = {
    key: idx for idx, stage in enumerate(STAGE_ORDER) for key in CONTEXT_KEYS[stage]
}

# Bounded value strategies — mix of scalars/strings/lists at sizes that span both sides of
# the ceiling (a single big list value can exceed 16KB on its own, forcing full eviction).
_scalars = st.one_of(st.booleans(), st.integers(-(10**6), 10**6), st.none())
_short_text = st.text(max_size=600)
_values = st.one_of(
    _scalars,
    _short_text,
    st.lists(_short_text, max_size=60),
    st.text(max_size=40_000),  # a lone value that can blow the ceiling by itself
)
_contexts = st.dictionaries(st.sampled_from(_ALL_KEYS), _values, max_size=len(_ALL_KEYS))


def _weight(key: str, value: object) -> int:
    return _context_bytes({key: value})


@_FAST
@given(_contexts)
def test_result_is_bounded_whitelisted_and_never_raises(context: dict) -> None:
    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    task.context = dict(context)
    original = dict(context)

    _enforce_context_ceiling(task)  # never raises on arbitrary string/size inputs

    # (i) result always lands at or under the ceiling
    assert _context_bytes(task.context) <= _MAX_CONTEXT_BYTES
    # only-shrinks: no key is invented, every survivor keeps its exact value (injective fold
    # preserved — nothing merged/overwritten), and every key stays inside the whitelist
    assert set(task.context) <= set(original)
    for key, value in task.context.items():
        assert value == original[key]
        assert key in _KEY_STAGE_INDEX


@_FAST
@given(_contexts)
def test_dropped_keys_are_a_priority_prefix(context: dict) -> None:
    """Trimming priority: no key is dropped while a same-or-higher-weight later-pipeline key
    survives. Equivalently, the dropped set is a prefix of the (heaviest-first, ties break
    later-stage-first) eviction order — a survivor never outranks a dropped key."""
    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    task.context = dict(context)

    _enforce_context_ceiling(task)

    survivors = set(task.context)
    dropped = set(context) - survivors
    for d in dropped:
        wd = _weight(d, context[d])
        for s in survivors:
            ws = _weight(s, context[s])
            # a strictly heavier key must never survive while a lighter one is dropped
            assert not (ws > wd), f"heavier {s} survived while lighter {d} dropped"
            # on an exact weight tie, the LATER-pipeline key is evicted first, so a later
            # survivor alongside an earlier drop would violate the tie-break
            if ws == wd:
                assert _KEY_STAGE_INDEX[s] <= _KEY_STAGE_INDEX[d], (
                    f"later-stage {s} survived while earlier {d} dropped on a weight tie"
                )


@_FAST
@given(_contexts)
def test_enforcement_is_idempotent(context: dict) -> None:
    once = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    once.context = dict(context)
    _enforce_context_ceiling(once)

    twice = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    twice.context = dict(context)
    _enforce_context_ceiling(twice)
    _enforce_context_ceiling(twice)  # enforcing again changes nothing

    assert once.context == twice.context


# --- integration: the same invariants through the real fold path (_absorb_outputs) --------
_fold_outputs = st.lists(
    st.tuples(
        st.sampled_from(list(STAGE_ORDER)),
        st.dictionaries(st.sampled_from(_ALL_KEYS), _values, max_size=len(_ALL_KEYS)),
    ),
    max_size=8,
)


@_FAST
@given(_fold_outputs)
def test_absorb_sequence_keeps_context_bounded_and_whitelisted(folds: list) -> None:
    """A random sequence of stage folds (the production call site) always leaves a bounded,
    whitelisted, injective context — every fold+enforce round-trip holds the invariants."""
    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    for stage, output in folds:
        _absorb_outputs(task, make_result_stub(Stage(stage), output))
        assert _context_bytes(task.context) <= _MAX_CONTEXT_BYTES
        for key in task.context:
            assert key in _KEY_STAGE_INDEX  # nothing outside the whitelist ever folded
