"""Per-task port-block allocator (#5): the allocator itself + its engine/runner wiring.

Covers the allocator (allocate/release round-trip, concurrent non-overlap, cross-instance
persistence, bind-probe skip, reclaim_stale) and the injection/lifecycle touches (opt-in
no-op, env into the deterministic test subprocess, the context-plane key flowing to a
downstream stage's WorkItem.env, and finalize releasing the block — including a raising
release never breaking finalize).
"""

from __future__ import annotations

import json
import socket
import threading
from datetime import UTC, datetime, timedelta

from adapters.execution.deterministic_test import DeterministicTestRunner
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.port_registry import (
    DEFAULT_BLOCK_SIZE,
    ENV_PORT,
    ENV_PORT_BASE,
    ENV_PORT_COUNT,
    Allocation,
    PortRegistry,
    port_env_for,
    project_needs_ports,
    registry_for_project,
)
from orchestrator.schemas.enums import (
    ExecutionLane,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
)
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result


def _reg(tmp_path, **kw) -> PortRegistry:
    kw.setdefault("bind_probe", False)  # locking/bookkeeping tests don't need real binds
    return PortRegistry(tmp_path / "port-registry.json", **kw)


def _iso_ago(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


# --- allocator: round-trip, persistence -------------------------------------------------

def test_allocate_release_round_trip(tmp_path) -> None:
    reg = _reg(tmp_path, port_range=(42000, 42099), block_size=10)
    base = reg.allocate("r1", "t1")
    assert base == 42000
    assert reg.allocation_for("r1", "t1").count == 10
    # A second, distinct task gets the NEXT aligned block (no overlap).
    assert reg.allocate("r1", "t2") == 42010
    # Idempotent: re-allocating the same pair returns the same base, no new block.
    assert reg.allocate("r1", "t1") == 42000
    # Release frees it; the freed base is handed out again to a fresh pair.
    assert reg.release("r1", "t1") is True
    assert reg.allocation_for("r1", "t1") is None
    assert reg.allocate("r1", "t3") == 42000
    # Releasing an unknown pair is a harmless no-op.
    assert reg.release("r1", "nope") is False


def test_persistence_across_instances(tmp_path) -> None:
    reg1 = _reg(tmp_path, port_range=(42000, 42099), block_size=10)
    base = reg1.allocate("r1", "t1")
    # A brand-new instance on the same file sees the allocation and won't reuse the block.
    reg2 = _reg(tmp_path, port_range=(42000, 42099), block_size=10)
    assert reg2.allocation_for("r1", "t1").base == base
    assert reg2.allocate("r1", "t1") == base  # idempotent across instances
    assert reg2.allocate("r1", "t2") != base  # new pair -> a different block


def test_exhausted_range_returns_none(tmp_path) -> None:
    reg = _reg(tmp_path, port_range=(42000, 42019), block_size=10)  # exactly 2 blocks
    assert reg.allocate("r", "a") == 42000
    assert reg.allocate("r", "b") == 42010
    assert reg.allocate("r", "c") is None  # nothing left


# --- allocator: concurrency -------------------------------------------------------------

def test_concurrent_allocations_never_overlap(tmp_path) -> None:
    # 20 threads each claim a distinct (run, task); the file lock must serialize the
    # read-modify-writes so no two claims land on the same block.
    reg = _reg(tmp_path, port_range=(42000, 42999), block_size=10)
    results: dict[str, int | None] = {}
    lock = threading.Lock()

    def claim(i: int) -> None:
        base = reg.allocate("run", f"task-{i}")
        with lock:
            results[f"task-{i}"] = base

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    bases = [b for b in results.values() if b is not None]
    assert len(bases) == 20
    assert len(set(bases)) == 20  # all distinct — no double-allocation under contention


# --- allocator: bind-probe --------------------------------------------------------------

def test_bind_probe_skips_an_occupied_base(tmp_path) -> None:
    # Actually occupy a port: bind a real socket, then point the registry's range at it with
    # block_size 1 so the first candidate IS the occupied port. The probe must skip it.
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    port = occupied.getsockname()[1]
    occupied.listen(1)
    try:
        reg = PortRegistry(
            tmp_path / "reg.json", port_range=(port, port + 5), block_size=1, bind_probe=True
        )
        base = reg.allocate("r", "t")
        assert base is not None
        assert base != port  # skipped the occupied port
        assert base > port
    finally:
        occupied.close()


# --- allocator: reclaim_stale -----------------------------------------------------------

def test_reclaim_stale_frees_terminal_and_aged_and_dead_pid(tmp_path) -> None:
    reg = _reg(tmp_path, port_range=(42000, 42999), block_size=10, ttl_s=100)
    # Seed the file directly (allocate() would prune stale rows before we can test reclaim).
    fresh = datetime.now(UTC).isoformat()
    reg._write([  # noqa: SLF001 - white-box seeding of the persisted records
        Allocation(42000, 10, "r", "terminal", None, fresh),   # terminal via callback
        Allocation(42010, 10, "r", "live", None, fresh),       # kept (live-ish, fresh)
        Allocation(42020, 10, "r", "dead", 999_999, fresh),    # pid dead -> reclaimed
        Allocation(42030, 10, "r", "aged", None, _iso_ago(10_000)),  # aged past TTL
    ])

    def is_terminal(run_id: str, task_id: str) -> bool:
        return task_id == "terminal"

    freed = reg.reclaim_stale(is_terminal)
    freed_tasks = {a.task_id for a in freed}
    assert freed_tasks == {"terminal", "dead", "aged"}
    assert reg.allocation_for("r", "live") is not None
    assert reg.allocation_for("r", "terminal") is None


def test_reclaim_stale_no_file_is_noop(tmp_path) -> None:
    reg = _reg(tmp_path / "missing", port_range=(42000, 42099))
    assert reg.reclaim_stale() == []


# --- opt-in / env helpers ---------------------------------------------------------------

class PortProject(FakeProject):
    """A project that OPTS INTO ports and maps a block onto its own server var names."""

    def __init__(self, registry_path) -> None:
        super().__init__()
        self.port_registry_path = str(registry_path)
        self.port_range = (42000, 42099)
        self.port_block_size = 10

    def port_env(self, base: int, count: int) -> dict[str, str]:
        return {"REACT_PORT": str(base), "APP_URL": f"http://localhost:{base}"}


def test_no_op_for_project_without_port_needs() -> None:
    # The stock fake exposes no port_env / needs_ports -> not opted in.
    assert project_needs_ports(FakeProject()) is False
    # ... while the generic env helper still yields the base trio for any base/count.
    env = port_env_for(FakeProject(), 42000, 10)
    assert env == {ENV_PORT_BASE: "42000", ENV_PORT_COUNT: "10", ENV_PORT: "42000"}


def test_needs_ports_attribute_opts_in() -> None:
    class NeedsPorts(FakeProject):
        needs_ports = True

    assert project_needs_ports(NeedsPorts()) is True


def test_port_env_hook_merges_over_the_generic_trio(tmp_path) -> None:
    proj = PortProject(tmp_path / "reg.json")
    assert project_needs_ports(proj) is True
    env = port_env_for(proj, 42010, 10)
    assert env[ENV_PORT_BASE] == "42010"
    assert env["REACT_PORT"] == "42010"
    assert env["APP_URL"] == "http://localhost:42010"


def test_registry_for_project_honors_overrides(tmp_path) -> None:
    proj = PortProject(tmp_path / "custom.json")
    reg = registry_for_project(proj)
    assert reg.path == tmp_path / "custom.json"
    assert (reg.lo, reg.hi) == (42000, 42099)
    assert reg.block_size == 10


# --- injection into the deterministic test runner ---------------------------------------

def _test_work(env: dict[str, str] | None) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.TEST,
        prompt="run tests", schema_ref="test", model="engine",
        lane_policy=LanePolicy(execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE),
        created_at=datetime.now(UTC).isoformat(),
        context={"baseline_failures": []}, env=env,
    )


def test_env_injection_lands_in_test_subprocess(tmp_path, monkeypatch) -> None:
    # Recording fake for the runner's subprocess.run: capture the env it was handed.
    seen: dict = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return _Proc()

    import adapters.execution.deterministic_test as dt
    monkeypatch.setattr(dt.subprocess, "run", fake_run)

    port_env = {ENV_PORT_BASE: "42010", ENV_PORT_COUNT: "10", ENV_PORT: "42010",
                "REACT_PORT": "42010"}
    work = _test_work(env=port_env).model_copy(update={"cwd": str(tmp_path)})
    # A project with a real test command so the runner actually shells one.
    result = DeterministicTestRunner(FakeProject()).dispatch(work)
    assert result.status is ResultStatus.SUCCESS
    assert seen["env"] is not None
    assert seen["env"][ENV_PORT_BASE] == "42010"
    assert seen["env"]["REACT_PORT"] == "42010"
    # The inherited process env is preserved (merged, not replaced).
    assert "PATH" in seen["env"]


def test_no_env_means_inherit_unchanged(tmp_path, monkeypatch) -> None:
    seen: dict = {"called": False}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["called"] = True
        seen["env"] = kwargs.get("env")
        return _Proc()

    import adapters.execution.deterministic_test as dt
    monkeypatch.setattr(dt.subprocess, "run", fake_run)
    work = _test_work(env=None).model_copy(update={"cwd": str(tmp_path)})
    DeterministicTestRunner(FakeProject()).dispatch(work)
    assert seen["called"] is True
    assert seen["env"] is None  # None => inherit the process env unchanged


# --- engine lifecycle: context plane + finalize release ---------------------------------

def _engine(tmp_path, project, **kw) -> Engine:
    store = StatusStore(tmp_path)
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    return Engine(store, ledger, project, **kw)


def _events(tmp_path) -> list[dict]:
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _run_intake_with_ports(eng: Engine, run="r1", task="t1", base=42010) -> None:
    """Drive the intake stage, simulating a runner result that carries a port block."""
    work = eng.next_work(run, task)
    assert work.stage is Stage.INTAKE
    out = {"branch": "issue-42", "worktree": f"/wt/{task}", "baseline_captured": True,
           "port_base": base, "port_count": 10}
    eng.record(run, make_result(work, structured_output=out))


def test_context_plane_port_base_flows_to_downstream_workitem_env(tmp_path) -> None:
    proj = PortProject(tmp_path / "reg.json")
    eng = _engine(tmp_path, proj)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    _run_intake_with_ports(eng, base=42010)

    # The intake fold put port_base/port_count on the context plane...
    task = eng.store.load_task("r1", "t1")
    assert task.context["port_base"] == 42010
    assert task.context["port_count"] == 10
    # ... and the NEXT stage's WorkItem carries the injected port env (project-mapped names).
    nxt = eng.next_work("r1", "t1")
    assert nxt.stage is not Stage.INTAKE
    assert nxt.env[ENV_PORT_BASE] == "42010"
    assert nxt.env["REACT_PORT"] == "42010"
    # ports_allocated was evented by the engine at intake.
    assert any(e["type"] == "ports_allocated" and e["port_base"] == 42010
               for e in _events(tmp_path))


def test_no_port_env_when_project_opts_out(tmp_path) -> None:
    eng = _engine(tmp_path, FakeProject())  # no port needs
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _run_intake_with_ports(eng, base=42010)  # even if a stray port_base appears...
    nxt = eng.next_work("r1", "t1")
    assert nxt.env is None  # ...an opted-out project never gets port env injected


def test_finalize_releases_the_block(tmp_path) -> None:
    proj = PortProject(tmp_path / "reg.json")
    # Pre-register the block the task will hold, as the real intake runner would have.
    registry_for_project(proj).allocate("r1", "t1", pid=None)
    assert registry_for_project(proj).allocation_for("r1", "t1") is not None

    eng = _engine(tmp_path, proj)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _run_intake_with_ports(eng, base=42000)
    # Drive the rest to completion.
    while (work := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(work))

    assert eng.store.load_task("r1", "t1").state.value == "completed"
    # The terminal transition released the block.
    assert registry_for_project(proj).allocation_for("r1", "t1") is None
    assert any(e["type"] == "ports_released" for e in _events(tmp_path))


def test_raising_release_never_breaks_finalize(tmp_path, monkeypatch) -> None:
    proj = PortProject(tmp_path / "reg.json")
    eng = _engine(tmp_path, proj)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _run_intake_with_ports(eng, base=42000)

    # Make the release path blow up; finalize must still complete the task.
    import orchestrator.engine as engine_mod

    def boom(_project):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(engine_mod, "registry_for_project", boom)
    while (work := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(work))

    assert eng.store.load_task("r1", "t1").state.value == "completed"
    assert any(e["type"] == "ports_release_failed" for e in _events(tmp_path))


def test_reclaim_stale_ports_is_noop_without_port_needs(tmp_path) -> None:
    eng = _engine(tmp_path, FakeProject())
    assert eng.reclaim_stale_ports("r1") == 0


def test_reclaim_stale_ports_frees_terminal_task_block(tmp_path) -> None:
    proj = PortProject(tmp_path / "reg.json")
    eng = _engine(tmp_path, proj)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    # A block for a task that never got cleaned up.
    registry_for_project(proj).allocate("r1", "t1", pid=None)
    # Force the task terminal in its store so reclaim's predicate flags it.
    task = eng.store.load_task("r1", "t1")
    task.state = task.state.__class__.COMPLETED
    eng.store.save_task(task)

    assert eng.reclaim_stale_ports("r1") == 1
    assert registry_for_project(proj).allocation_for("r1", "t1") is None


def test_default_block_size_constant() -> None:
    assert DEFAULT_BLOCK_SIZE == 10
