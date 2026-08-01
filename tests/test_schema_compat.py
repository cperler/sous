"""#275 — ``schema_version`` must PROTECT compatibility, not merely document intent.

Before this, the field was an unconstrained string that no reader enforced. Three holes
followed, and each has a test below:

* a status doc marked ``"999"`` — written by an engine from the future — loaded fine, and
  the next write serialized this engine's lossy view back over it, dropping whatever the
  newer engine had stored while faithfully preserving the misleading version number;
* Pydantic's default ``extra="ignore"`` meant an unknown persisted key was silently
  dropped at load on both the status and work planes;
* ``run_targets/workflow_shim.js`` emitted StageResult ``schema_version: '1'`` against a
  newer engine, and nothing noticed because ``record()`` never read the field.

The policy is asymmetric by plane and that asymmetry is the point: STATUS docs are an
archive, so old versions migrate forward and only the future is refused; the WORK plane is
in-flight wire traffic, so exactly one version is supported in both directions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from adapters.execution.interactive import build_stage_result
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import (
    ContractError,
    RunExistsError,
    SchemaVersionError,
    StatusStoreError,
)
from orchestrator.schemas.enums import (
    MIGRATABLE_STATUS_VERSIONS,
    SCHEMA_VERSION,
    SUPPORTED_STATUS_VERSIONS,
    SUPPORTED_WORK_VERSIONS,
    ExecutionLane,
    Stage,
    StageStatus,
    is_future_version,
)
from orchestrator.schemas.status import Run, Task, TaskRef
from orchestrator.schemas.work import StageResult, TokenUsage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

_ROOT = Path(__file__).resolve().parent.parent
SHIM = _ROOT / "run_targets" / "workflow_shim.js"
DRIVER = Path(__file__).resolve().parent / "_shim_driver.mjs"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _run_doc(run_id: str = "r1") -> dict:
    ts = _now()
    return json.loads(
        Run(
            run_id=run_id,
            created_at=ts,
            updated_at=ts,
            task_refs=[TaskRef(task_id="t1", status_file=f"status-{run_id}-t1.json")],
            dependency_graph={"t1": []},
        ).model_dump_json()
    )


def _task_doc(run_id: str = "r1", task_id: str = "t1") -> dict:
    ts = _now()
    return json.loads(
        Task(task_id=task_id, run_id=run_id, created_at=ts, updated_at=ts).model_dump_json()
    )


def _write(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


# --- migrating the known-old versions -------------------------------------------------


@pytest.mark.parametrize("version", MIGRATABLE_STATUS_VERSIONS)
def test_every_migratable_run_version_loads_and_stamps_current(tmp_path, version) -> None:
    """One explicit migration case per supported older version — including "0", the
    synthetic name for a doc predating the field entirely."""
    store = StatusStore(tmp_path)
    doc = _run_doc()
    if version == "0":
        doc.pop("schema_version")
    else:
        doc["schema_version"] = version

    _write(tmp_path / "status-r1.json", doc)
    assert store.load_run("r1").schema_version == SCHEMA_VERSION


@pytest.mark.parametrize("version", MIGRATABLE_STATUS_VERSIONS)
def test_every_migratable_task_version_loads_and_stamps_current(tmp_path, version) -> None:
    store = StatusStore(tmp_path)
    doc = _task_doc()
    if version == "0":
        doc.pop("schema_version")
    else:
        doc["schema_version"] = version

    _write(tmp_path / "status-r1-t1.json", doc)
    loaded = store.load_task("r1", "t1")
    assert loaded.schema_version == SCHEMA_VERSION
    # v1 -> v2 gained `pipeline`; the migration stamps the version and the model's own
    # lane-preset validator supplies the field, so the doc really is current-shaped.
    assert loaded.pipeline


def test_migration_is_durable_on_the_next_write(tmp_path) -> None:
    """The upgraded version must reach DISK, not just the in-memory model — otherwise the
    doc re-migrates on every load and a reader inspecting the file still sees v1."""
    store = StatusStore(tmp_path)
    doc = _task_doc()
    doc["schema_version"] = "1"
    _write(tmp_path / "status-r1-t1.json", doc)

    store.update_task("r1", "t1", lambda t: setattr(t, "attempt", 1))

    persisted = json.loads((tmp_path / "status-r1-t1.json").read_text())
    assert persisted["schema_version"] == SCHEMA_VERSION


def test_v3_task_gains_the_simplify_stage_record_on_load(tmp_path) -> None:
    """v4 extends the stage vocabulary; a real v3 map lacks the new key."""
    store = StatusStore(tmp_path)
    doc = _task_doc()
    doc["schema_version"] = "3"
    doc["stages"].pop("simplify")
    _write(tmp_path / "status-r1-t1.json", doc)

    loaded = store.load_task("r1", "t1")
    assert loaded.stages[Stage.SIMPLIFY].status is StageStatus.PENDING
    assert Stage.SIMPLIFY not in loaded.pipeline  # old runs keep their exact sequence


def test_the_ladder_covers_every_version_this_engine_accepts(tmp_path) -> None:
    """Guard against a future SCHEMA_VERSION bump that forgets the ladder: the supported
    set is exactly the migratable versions plus the current one, so a bump to "4" without
    adding "3" to MIGRATABLE_STATUS_VERSIONS fails HERE rather than on a user's v3 run."""
    expected = {*MIGRATABLE_STATUS_VERSIONS, SCHEMA_VERSION}
    assert set(SUPPORTED_STATUS_VERSIONS) == expected
    assert SCHEMA_VERSION not in MIGRATABLE_STATUS_VERSIONS


# --- failing closed on the future -----------------------------------------------------


def test_future_version_run_doc_is_refused_and_left_byte_identical(tmp_path) -> None:
    store = StatusStore(tmp_path)
    doc = _run_doc()
    doc["schema_version"] = "999"
    doc["some_future_field"] = {"the newer engine": "stored this"}
    path = tmp_path / "status-r1.json"
    _write(path, doc)
    before = path.read_bytes()

    with pytest.raises(SchemaVersionError, match="999"):
        store.load_run("r1")

    assert path.read_bytes() == before


def test_future_version_task_doc_cannot_be_mutated(tmp_path) -> None:
    """The write paths all load first, so refusing at READ means no mutation ever begins —
    this is what makes "fail closed" a property of the store rather than of each caller."""
    store = StatusStore(tmp_path)
    doc = _task_doc()
    doc["schema_version"] = "999"
    doc["future_only_field"] = 7
    path = tmp_path / "status-r1-t1.json"
    _write(path, doc)
    before = path.read_bytes()

    with pytest.raises(SchemaVersionError):
        store.update_task("r1", "t1", lambda t: setattr(t, "attempt", 99))

    assert path.read_bytes() == before
    # The unknown field is still there for the engine that owns it — the whole point.
    assert json.loads(path.read_text())["future_only_field"] == 7


def test_future_version_run_doc_cannot_be_clobbered_by_create_either(tmp_path) -> None:
    """The other way to lose a future doc is to write a FRESH one over it. ``create_run_doc``
    refuses on existence (#280) before it can read the version, so the two guards compose:
    update refuses on version, create refuses on existence, and neither path can leave a
    newer engine's run half-overwritten."""
    store = StatusStore(tmp_path)
    doc = _run_doc()
    doc["schema_version"] = "999"
    path = tmp_path / "status-r1.json"
    _write(path, doc)
    before = path.read_bytes()

    with pytest.raises(RunExistsError):
        store.create_run_doc(
            Run(run_id="r1", created_at=_now(), updated_at=_now())
        )

    assert path.read_bytes() == before


def test_unparseable_version_is_refused_too(tmp_path) -> None:
    """Not future, not on the ladder — unorderable garbage is still not readable."""
    store = StatusStore(tmp_path)
    doc = _run_doc()
    doc["schema_version"] = "banana"
    _write(tmp_path / "status-r1.json", doc)

    with pytest.raises(SchemaVersionError, match="recognize"):
        store.load_run("r1")


def test_refusal_is_a_status_store_error_but_not_a_not_found(tmp_path) -> None:
    """``run_exists`` narrows to StatusNotFoundError so real failures are not read as
    "absent" (#112). A future-version doc EXISTS; reporting it missing would invite a
    caller to create a fresh run straight over it."""
    store = StatusStore(tmp_path)
    doc = _run_doc()
    doc["schema_version"] = "999"
    _write(tmp_path / "status-r1.json", doc)

    assert issubclass(SchemaVersionError, StatusStoreError)
    with pytest.raises(SchemaVersionError):
        store.run_exists("r1")


def test_version_ordering_is_numeric_not_lexicographic() -> None:
    """"10" must read as newer than "9"; a string compare gets that backwards the first
    time the major version hits double digits."""
    assert is_future_version("999")
    assert not is_future_version(SCHEMA_VERSION)
    assert not is_future_version("1")
    assert not is_future_version("banana")  # unorderable, not future


# --- unknown fields: deliberate, on both planes ---------------------------------------


def test_unknown_field_in_a_current_version_status_doc_is_refused(tmp_path) -> None:
    """Same-version docs cannot smuggle a key past the version gate: a doc claiming to be
    v3 while carrying a field v3 does not define is malformed, and ignoring the key would
    delete it on the next write exactly as before."""
    store = StatusStore(tmp_path)
    doc = _task_doc()
    doc["typoed_or_invented_key"] = "silently dropped before #275"
    _write(tmp_path / "status-r1-t1.json", doc)

    with pytest.raises(ValidationError, match="typoed_or_invented_key"):
        store.load_task("r1", "t1")


def test_unknown_field_in_a_stage_result_is_refused() -> None:
    """The work plane is a cross-language seam — hand-assembled JS/shell result JSON is
    where an invented key actually happens."""
    payload = json.loads(
        make_result_stub().model_dump_json()
    )
    payload["structured_ouptut"] = {"typo": "of structured_output"}

    with pytest.raises(ValidationError, match="structured_ouptut"):
        StageResult.model_validate(payload)


def test_free_form_payload_fields_still_accept_arbitrary_keys(tmp_path) -> None:
    """extra="forbid" must bite only on the DECLARED shape: the deliberately open payloads
    (Task.context, StageRecord.output) are typed dicts and stay open."""
    store = StatusStore(tmp_path)
    task = Task(task_id="t1", run_id="r1", created_at=_now(), updated_at=_now())
    task.context = {"anything": {"nested": [1, 2, 3]}}
    task.stages[Stage.SCOPE].output = {"whatever": "the stage returned"}
    store.save_task(task)

    loaded = store.load_task("r1", "t1")
    assert loaded.context == {"anything": {"nested": [1, 2, 3]}}
    assert loaded.stages[Stage.SCOPE].output == {"whatever": "the stage returned"}


def make_result_stub() -> StageResult:
    """A minimal valid StageResult (no engine needed) for pure-model assertions."""
    from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
    from orchestrator.schemas.work import LaneUsed

    return StageResult(
        work_item_id="wi-1",
        content_hash="a" * 64,
        run_id="r1",
        task_id="t1",
        stage=Stage.SCOPE,
        model="claude-opus-5",
        status=ResultStatus.SUCCESS,
        structured_output={"feasible": True, "plan": ["x"]},
        lane_used=LaneUsed(
            execution_mode=ExecutionMode.INTERACTIVE,
            provider=Provider.CLAUDE,
            invocation="agent()",
        ),
        token_usage=TokenUsage(),
        completed_at=_now(),
    )


# --- the work plane's version, enforced at the engine boundary ------------------------


def _engine(tmp_path, project) -> Engine:
    return Engine(
        StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project
    )


def _scope_dispatch(eng: Engine):
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # deterministic intake
    work = eng.next_work("r1", "t1")
    assert work.stage is Stage.SCOPE
    return work


def test_record_refuses_an_off_version_stage_result(tmp_path, project) -> None:
    """The shipped bug, made loud: a runner emitting the old ``'1'`` contract."""
    eng = _engine(tmp_path, project)
    work = _scope_dispatch(eng)
    stale = make_result(work).model_copy(update={"schema_version": "1"})

    with pytest.raises(ContractError, match="schema_version"):
        eng.record("r1", stale)

    # Nothing landed, nothing was charged, and the lease survives so the correct result
    # can still be recorded once the runner is fixed.
    task = eng.store.load_task("r1", "t1")
    assert task.stages[Stage.SCOPE].status is StageStatus.RUNNING
    assert task.pending_work_item_id == work.id
    assert eng.ledger.existing_rows_for(work.id) == []


def test_off_version_refusal_is_evented_with_its_reason_code(tmp_path, project) -> None:
    """Never silent: the refusal belongs in events.jsonl, not only on stderr (#311)."""
    eng = _engine(tmp_path, project)
    work = _scope_dispatch(eng)
    future = make_result(work).model_copy(update={"schema_version": "999"})

    with pytest.raises(ContractError):
        eng.record("r1", future)

    rejected = [e for e in eng.store.read_events("r1") if e["type"] == "result_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["level"] == "warning"
    assert rejected[0]["reason"] == "schema_version_unsupported"


def test_version_is_checked_before_the_lease_fields(tmp_path, project) -> None:
    """Ordering matters for the ERROR a human gets: when the two sides disagree about what
    a StageResult is, "your runner is off-contract" is actionable and "content_hash
    mismatch" is a red herring."""
    eng = _engine(tmp_path, project)
    work = _scope_dispatch(eng)
    both_wrong = make_result(work).model_copy(
        update={"schema_version": "1", "content_hash": "b" * 64}
    )

    with pytest.raises(ContractError, match="schema_version"):
        eng.record("r1", both_wrong)


def test_a_current_version_result_still_records(tmp_path, project) -> None:
    """The gate must not be a wall: the normal path is unaffected."""
    eng = _engine(tmp_path, project)
    work = _scope_dispatch(eng)
    assert make_result(work).schema_version == SCHEMA_VERSION

    eng.record("r1", make_result(work))
    assert eng.store.load_task("r1", "t1").stages[Stage.SCOPE].status is StageStatus.COMPLETED


# --- one version across both result builders ------------------------------------------


def test_python_result_builders_emit_the_current_version() -> None:
    """The interactive lane's Python mirror (adapters/execution/interactive.py) and the
    WorkItem the engine emits both carry the one supported wire version."""
    from orchestrator.schemas.enums import ExecutionMode, Provider
    from orchestrator.schemas.work import LanePolicy, WorkItem

    work = WorkItem(
        id="wi-1",
        content_hash="a" * 64,
        run_id="r1",
        task_id="t1",
        stage=Stage.SCOPE,
        prompt="p",
        schema_ref="scope",
        model="claude-opus-5",
        lane_policy=LanePolicy(
            execution_mode=ExecutionMode.INTERACTIVE, provider=Provider.CLAUDE
        ),
        created_at=_now(),
    )
    assert work.schema_version == SCHEMA_VERSION
    one_wire_version = {SCHEMA_VERSION}
    assert set(SUPPORTED_WORK_VERSIONS) == one_wire_version

    built = build_stage_result(
        work_item=work,
        structured_output={"feasible": True, "plan": ["x"]},
        usage=TokenUsage(),
        completed_at=_now(),
    )
    assert built.schema_version == SCHEMA_VERSION


def test_shim_fallback_literal_matches_the_engine_version() -> None:
    """The JS shim cannot import SCHEMA_VERSION, so its fallback literal is pinned here —
    this assertion is exactly what was missing when the shim sat on '1' for three
    versions. The shim ECHOES wi.schema_version in practice (test below); this guards the
    one path where a WorkItem arrives without the field."""
    src = SHIM.read_text(encoding="utf-8")
    line = next(ln for ln in src.splitlines() if "schema_version:" in ln)
    assert f"'{SCHEMA_VERSION}'" in line, (
        f"workflow_shim.js fallback is stale: {line.strip()!r} does not carry "
        f"SCHEMA_VERSION {SCHEMA_VERSION!r}"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_shim_echoes_the_dispatching_work_items_version() -> None:
    """Driven through node so this is the real shim: the result's version comes from the
    WorkItem, so the lane cannot drift from the engine that dispatched it."""
    proc = subprocess.run(  # noqa: S603
        [shutil.which("node"), str(DRIVER), str(SHIM)],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)

    # wi-1 carries an explicit (hypothetical future) version -> echoed verbatim;
    # wi-2 carries none -> the pinned fallback.
    assert data["resultSchemaVersion"] == {"wi-1": "77", "wi-2": SCHEMA_VERSION}
