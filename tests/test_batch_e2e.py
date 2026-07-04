"""Batch-lane END-TO-END integration harness (#35).

The single-task pipeline is live-proven, but the batch/DAG scheduler
(``orchestrator/scheduler.py``) has only ever run under unit tests with fake stores
and hand-built results. This suite exercises the scheduler loop against REAL
infrastructure — everything short of a live model call:

  * a real ``git`` project repo (temp) with real ``git worktree`` intake,
  * a real ``StatusStore`` + ``CostLedger`` on tmp_path,
  * a ``ProjectConfig`` whose commands are real subprocesses (the unit-test command
    is a tiny shell script that actually runs in the task worktree),
  * the REAL deterministic ENGINE-lane executors for INTAKE / TEST / DELIVER
    (``gh`` stubbed through the deliver runner's injectable subprocess seam),
  * only the model stages (SCOPE / IMPLEMENT / REVIEW) are SCRIPTED — and even those
    are REAL-EFFECT: the scripted IMPLEMENT actually writes a file and commits it in
    the task worktree, so the deterministic TEST really passes/fails and the
    deterministic DELIVER really finds commits to open a PR for.

Everything is driven through the actual ``Scheduler.run()`` loop with a fake sleeper —
never a hand-rolled tick loop — so the scenarios catch integration bugs (path/CWD
assumptions, context-plane gaps between real deterministic and scripted stages,
worktree issues, serialization) that fake-store unit tests can't.

The human-gated live MODEL batch against the product repo (heysoo) is deliberately
NOT here — that is Craig's call per the CLAUDE.md hard checkpoint.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from adapters.execution.deterministic_deliver import DeterministicDeliverRunner
from adapters.execution.deterministic_setup import DeterministicSetupRunner
from adapters.execution.deterministic_test import DeterministicTestRunner
from adapters.execution.runners import build_registry
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.failure_classifier import Failure
from orchestrator.routing import Router
from orchestrator.scheduler import Scheduler
from orchestrator.schemas.enums import ResultStatus, Stage
from orchestrator.status_store import StatusStore
from tests.conftest import FakeTaskSource, make_result

# All tasks opt TEST + DELIVER into the $0 deterministic ENGINE lane (INTAKE always is),
# so INTAKE/TEST/DELIVER exercise the REAL executors and only SCOPE/IMPLEMENT/REVIEW are
# scripted model stages.
_DET = (Stage.TEST, Stage.DELIVER)

# The project's unit-test command: a real shell script run in the task worktree. It is
# RED exactly when the scripted IMPLEMENT planted a BROKEN sentinel (and committed it),
# so one task's tests genuinely fail while another's pass — no faked test verdicts.
_UNIT_CMD = ["sh", "-c", "if [ -f BROKEN ]; then echo 'FAILED broken::unit'; exit 1; fi; echo ok"]


class _Classifier:
    """Parses ``FAILED <id>`` lines into unit failures (the project's FailureClassifier)."""

    def classify(self, test_output: str) -> list[Failure]:
        return [
            Failure(test=line.split(" ", 1)[1], kind="unit")
            for line in test_output.splitlines()
            if line.startswith("FAILED ")
        ]

    def impacted_tests(self, changed_files: list[str]) -> list[str]:
        return []


class E2EProject:
    """A real ProjectConfig: real shell commands, a parsing classifier, a task source,
    and a recording ``notify`` hook. Deliberately does NOT define ``setup_task`` — so the
    ENGINE lane runs the REAL ``git worktree`` intake, not the no-git conftest fake."""

    name = "e2e"

    def __init__(self, repo_root: str | None = None) -> None:
        self._classifier = _Classifier()
        self._task_source = FakeTaskSource()
        self.notifications: list[tuple[str, dict]] = []
        # #42: the explicit product-repo path the deterministic INTAKE runner discovers
        # the repo from. When None the runner falls back to process CWD.
        self.repo_root = repo_root

    def install_cmd(self):
        return ["sh", "-c", "exit 0"]  # real subprocess, no-op

    def test_unit_cmd(self, files=None):
        return list(_UNIT_CMD)

    def test_e2e_cmd(self, files=None):
        return None

    def test_shell_cmd(self, files=None):
        return None

    def typecheck_cmd(self):
        return ["true"]

    def infra_reset(self):
        return ["true"]

    @property
    def classifier(self):
        return self._classifier

    @property
    def task_source(self):
        return self._task_source

    def agent_for(self, stage: Stage, role: str | None = None):
        return None

    def notify(self, kind: str, payload: dict) -> None:
        self.notifications.append((kind, payload))


class GhStub:
    """Injectable subprocess runner for the DELIVER executor: runs REAL git for the
    read-only local queries (rev-parse / rev-list, so commit counting is genuine) but
    stubs the network verbs — ``git push`` succeeds without a remote, ``gh pr create``
    returns a synthetic PR url (recorded), ``gh pr list`` reports no existing PR."""

    def __init__(self) -> None:
        self.prs: list[tuple[str | None, str]] = []
        self._n = 1000

    def __call__(self, argv, cwd=None, **_kw):
        if argv and argv[0] == "git" and "push" not in argv:
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)
        if "push" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "pr", "create"]:
            self._n += 1
            url = f"https://github.com/x/y/pull/{self._n}"
            self.prs.append((cwd, url))
            return subprocess.CompletedProcess(argv, 0, url + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class ScriptedLane:
    """The Scheduler ``Runner``: dispatches each WorkItem by lane/stage. Deterministic
    stages hit the REAL executors; model stages are scripted-but-real-effect."""

    def __init__(
        self,
        project: E2EProject,
        gh: GhStub,
        *,
        break_tasks: set[str] | None = None,
        infeasible_tasks: set[str] | None = None,
        rate_limit_once: set[tuple[str, Stage]] | None = None,
    ) -> None:
        self.project = project
        self.gh = gh
        self.break_tasks = set(break_tasks or ())
        self.infeasible_tasks = set(infeasible_tasks or ())
        self.rate_limit_once: dict[tuple[str, Stage], int] = dict.fromkeys(rate_limit_once or (), 1)
        self.seen: list[tuple[str, str]] = []  # (task_id, stage) dispatch order
        self._setup = DeterministicSetupRunner(project)

    def __call__(self, workitems):
        return [self._one(w) for w in workitems]

    def _one(self, w):
        self.seen.append((w.task_id, w.stage.value))
        if w.stage is Stage.INTAKE:
            return self._setup.dispatch(w)  # REAL git worktree + install + baseline
        if w.stage is Stage.TEST:
            return DeterministicTestRunner(self.project).dispatch(w)  # REAL subprocess test run
        if w.stage is Stage.DELIVER:
            return DeterministicDeliverRunner(self.project, runner=self.gh).dispatch(w)  # REAL git + stubbed gh
        # --- scripted model stages (real-effect) ---------------------------------
        key = (w.task_id, w.stage)
        if self.rate_limit_once.get(key, 0) > 0:
            self.rate_limit_once[key] -= 1
            return make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={})
        if w.stage is Stage.SCOPE:
            if w.task_id in self.infeasible_tasks:
                return make_result(
                    w, structured_output={"feasible": False, "blocked_reason": "e2e: infeasible by script"}
                )
            return make_result(w, structured_output={"feasible": True, "plan": ["do the thing"]})
        if w.stage is Stage.IMPLEMENT:
            self._implement(w)
            return make_result(
                w, structured_output={"files_changed": ["change.txt"], "summary": "impl", "committed": True}
            )
        if w.stage is Stage.REVIEW:
            return make_result(w, structured_output={"approved": True, "issues": []})
        return make_result(w)

    def _implement(self, w) -> None:
        """REAL effect: write a file (and BROKEN for the failing tasks) and commit it in
        the task worktree folded from intake — so the deterministic TEST/DELIVER see genuine
        commits/state, not a faked structured_output."""
        wt = w.cwd
        assert wt, "implement dispatched without a folded worktree cwd (context-plane gap)"
        Path(wt, "change.txt").write_text(f"impl {w.task_id} attempt {w.attempt}\n")
        if w.task_id in self.break_tasks:
            Path(wt, "BROKEN").write_text("planted red\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", f"impl {w.task_id}"],
            cwd=wt, check=True,
        )


# --- fixtures / helpers -----------------------------------------------------------

def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("baseline\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _engine(tmp_path: Path, project: E2EProject, *, router: Router | None = None, **kw) -> Engine:
    registry = build_registry(setup_project=project)
    return Engine(
        StatusStore(tmp_path / "store"),
        CostLedger(tmp_path / "stage-costs.jsonl"),
        project,
        registry=registry,
        router=router or Router(),
        **kw,
    )


def _events(eng: Engine, run: str, kind: str) -> list[dict]:
    return [e for e in eng.store.read_events(run) if e["type"] == kind]


# --- scenario a: 3-task DAG runs to completion in dependency order ----------------

def test_a_three_task_dag_completes_in_dependency_order(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    # #42 fallback case (deliberately kept): no explicit repo_root, so intake discovers
    # the repo from process CWD — the legacy "orchestrator runs from the product repo" path.
    monkeypatch.chdir(repo)
    project = E2EProject()
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", deterministic_stages=_DET)
    eng.add_task("r1", "t2", depends_on=["t1"], deterministic_stages=_DET)  # gated behind t1
    eng.add_task("r1", "t3", deterministic_stages=_DET)  # independent

    gh = GhStub()
    lane = ScriptedLane(project, gh)
    status = Scheduler(eng, max_concurrent=3).run("r1", lane)

    assert status["run_state"] == "completed"
    assert all(status["tasks"][t]["state"] == "completed" for t in ("t1", "t2", "t3"))
    # dependency order: t2's FIRST dispatch must follow t1's LAST.
    t1_last = max(i for i, (t, _s) in enumerate(lane.seen) if t == "t1")
    t2_first = min(i for i, (t, _s) in enumerate(lane.seen) if t == "t2")
    assert t2_first > t1_last
    # a real PR was 'opened' per task (recorded by the gh stub), each in its own worktree.
    assert len(gh.prs) == 3
    assert len({url for _cwd, url in gh.prs}) == 3
    assert all(status["tasks"][t]["pr_url"] for t in ("t1", "t2", "t3"))
    # real worktrees were created on disk by the real intake executor.
    assert all((repo / ".worktrees" / t).is_dir() for t in ("t1", "t2", "t3"))
    # ledger rows are present and coherent: 3 tasks x 6 stages, lane audit clean.
    assert status["lane_audit"]["total_calls"] == 18
    assert status["lane_audit"]["clean"] is True
    assert len(_events(eng, "r1", "task_completed")) == 3
    fin = _events(eng, "r1", "run_finalized")
    assert fin and fin[-1]["state"] == "completed"


# --- scenario b: genuine red TEST → breaker → unpause → surviving task completes ---

def test_b_failing_tests_trip_breaker_then_unpause_resumes(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))  # #42: explicit path, no chdir needed
    # max_attempts=1: one implement→(red)test cycle fails the task terminally.
    eng = _engine(tmp_path, project, max_attempts=1, breaker_threshold=9)
    eng.create_run("r1")
    for t in ("t0", "t1", "t2"):
        eng.add_task("r1", t, deterministic_stages=_DET)

    gh = GhStub()
    # t0 + t1 plant a BROKEN sentinel → the REAL deterministic TEST genuinely fails.
    lane = ScriptedLane(project, gh, break_tasks={"t0", "t1"})
    sched = Scheduler(eng, max_concurrent=1, batch_failure_threshold=2)
    status = sched.run("r1", lane)

    # two consecutive genuine test failures trip the batch breaker → PAUSED, t2 untouched.
    assert status["run_state"] == "paused"
    assert status["tasks"]["t0"]["state"] == "failed"
    assert status["tasks"]["t1"]["state"] == "failed"
    assert status["tasks"]["t2"]["state"] == "pending"  # budget survived the stop
    paused = _events(eng, "r1", "run_paused")
    assert paused and "circuit breaker" in paused[-1]["reason"]
    # the failures were REAL deterministic-test red (a genuine subprocess exit≠0 that the
    # ENGINE-lane TEST runner classified), not a faked FAILURE status: the recorded error
    # is the deterministic test runner's caused-failure message and the TEST stage failed.
    t0 = eng.store.load_task("r1", "t0")
    assert "test stage red" in (t0.last_error or "") and t0.stages[Stage.TEST].status.value == "failed"

    # a paused run refuses to schedule…
    again = Scheduler(eng, max_concurrent=1, batch_failure_threshold=2).run("r1", lane)
    assert again["run_state"] == "paused"

    # …until unpaused; then the surviving (healthy) task runs to green completion.
    eng.unpause_run("r1")
    final = Scheduler(eng, max_concurrent=1, batch_failure_threshold=2).run("r1", lane)
    assert final["run_state"] == "failed"  # t0/t1 stay terminally failed…
    assert final["tasks"]["t2"]["state"] == "completed"  # …but t2 completed after resume
    assert final["tasks"]["t2"]["pr_url"]


# --- scenario c: reject a held task mid-batch → CLOSED_INFEASIBLE + dependent handled -

def test_c_reject_held_task_cascades_dependents_without_tripping_breaker(tmp_path) -> None:
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))  # #42: explicit path, no chdir needed
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "ta", deterministic_stages=_DET)  # SCOPE says infeasible → held
    eng.add_task("r1", "tb", depends_on=["ta"], deterministic_stages=_DET)  # dependent of ta
    eng.add_task("r1", "tc", deterministic_stages=_DET)  # independent → completes

    gh = GhStub()
    lane = ScriptedLane(project, gh, infeasible_tasks={"ta"})
    status = Scheduler(eng, max_concurrent=3).run("r1", lane)

    # ta parked at the human gate; tc finished; tb still waiting on ta. Run NOT finalized.
    assert status["run_state"] == "running"
    assert status["tasks"]["ta"]["state"] == "blocked_on_human"
    assert status["tasks"]["tc"]["state"] == "completed"
    assert status["tasks"]["tb"]["state"] == "pending"

    # the human confirms ta is infeasible → reject mid-batch.
    eng.reject("r1", "ta", rejected_by="craig", reason="not worth doing")

    final = eng.status("r1")
    assert final["tasks"]["ta"]["state"] == "closed_infeasible"
    # DEPENDENT-OF-REJECTED behavior (the 2c question): reject reuses the FAILED cascade
    # path, so a dependent of a closed-infeasible task is transitively CASCADE_BLOCKED —
    # defined and reasonable (a dependent of an infeasible task can't proceed).
    assert final["tasks"]["tb"]["state"] == "cascade_blocked"
    assert final["tasks"]["tc"]["state"] == "completed"
    assert final["run_state"] == "failed"  # a closed-infeasible task rolls the run up to failed
    # the breaker was NOT tripped: reject is an out-of-band close, never an execution failure.
    assert _events(eng, "r1", "run_paused") == []
    assert final["tasks"]["ta"].get("rejection_reason") == "not worth doing"


# --- scenario d: rate-limit cooldown → scheduler waits it out, task retries ----------

def test_d_rate_limit_cooldown_is_waited_out_then_retries(tmp_path) -> None:
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))  # #42: explicit path, no chdir needed
    # allow_fallback=False → a rate limit can't be dodged with a cheaper model, so it takes
    # the cooldown path (not_before stamped) that the scheduler must wait out.
    eng = _engine(tmp_path, project, router=Router(allow_fallback=False), rate_limit_cooldown_s=120)
    eng.create_run("r1")
    eng.add_task("r1", "t1", deterministic_stages=_DET)

    gh = GhStub()
    lane = ScriptedLane(project, gh, rate_limit_once={("t1", Stage.SCOPE)})

    slept: list[int] = []

    def sleeper(secs: int) -> None:
        slept.append(secs)
        # the cooldown window elapsing: clear the stamp so the next probe re-dispatches.
        eng.store.update_task("r1", "t1", lambda t: setattr(t, "not_before", None))

    status = Scheduler(eng, max_concurrent=1).run("r1", lane, sleeper=sleeper)

    assert slept and 0 < slept[0] <= 121  # slept the cooldown remainder, then resumed
    cooldown = _events(eng, "r1", "rate_limit_cooldown")
    assert cooldown and cooldown[0]["stage"] == "scope"
    assert status["run_state"] == "completed"
    assert status["tasks"]["t1"]["state"] == "completed"


# --- scenario e: stale alerting + dedupe fire during the scheduler loop --------------

def test_e_stale_alert_fires_once_during_the_loop(tmp_path) -> None:
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))  # #42: explicit path, no chdir needed
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "stuck", deterministic_stages=_DET)
    eng.add_task("r1", "worker", deterministic_stages=_DET)

    # Park 'stuck' with an outstanding dispatch lease (as a crashed mid-stage task) and age
    # it far past the stale threshold — non-terminal, non-dispatchable, and stale.
    eng.next_work("r1", "stuck")  # takes the lease; never recorded
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    stuck = eng.store.load_task("r1", "stuck")  # save_task (not update_task) preserves the
    stuck.updated_at = old  # aged timestamp — update_task re-stamps updated_at to now().
    eng.store.save_task(stuck)

    gh = GhStub()
    lane = ScriptedLane(project, gh)
    # 'worker' runs to completion over several ticks; each loop pass re-polls staleness.
    Scheduler(eng, max_concurrent=1).run("r1", lane, stale_after_s=1)

    stale = [p for (kind, p) in project.notifications if kind == "task_stale"]
    assert len(stale) == 1  # fired once per stall episode (dedupe held across every tick)
    assert stale[0]["task_id"] == "stuck"
    # dedupe is also visible in the durable audit trail: exactly one task_stale row.
    assert len(_events(eng, "r1", "notification")) >= 1
    assert len([e for e in _events(eng, "r1", "notification") if e["kind"] == "task_stale"]) == 1
    # the worker really did complete through the real executors while 'stuck' stalled.
    assert eng.status("r1")["tasks"]["worker"]["state"] == "completed"
