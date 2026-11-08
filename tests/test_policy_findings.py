"""Deterministic review policy gates (#65): the adapter's review_findings hook merges
engine-side into a completed REVIEW — a blocking finding overrides the model's
approval (the old merge_e2e_policy_review_finding semantics), advisory findings
become tracked follow-ups, and a raising hook can never break record()."""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.status import Task
from orchestrator.schemas.work import LaneUsed, StageResult, TokenUsage
from orchestrator.state_machine import _MAX_BASELINE_LINT, _absorb_outputs
from orchestrator.status_store import StatusStore
from tests.conftest import REVIEW_PROOF, FakeProject, make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _advance_to_review(eng, run="r1", task="t1", intake=None):
    eng.create_run(run)
    eng.add_task(run, task)
    for _ in range(5):
        work = eng.next_work(run, task)
        if work.stage is Stage.INTAKE and intake is not None:
            eng.record(run, make_result(work, structured_output=intake))
        else:
            eng.record(run, make_result(work))
    w = eng.next_work(run, task)
    assert w.stage is Stage.REVIEW
    return w


class _PolicyProject(FakeProject):
    def __init__(self, findings) -> None:
        super().__init__()
        self._findings = findings
        self.seen_worktrees: list = []

    def review_findings(self, *, worktree=None):
        self.seen_worktrees.append(worktree)
        if isinstance(self._findings, Exception):
            raise self._findings
        return self._findings


def test_blocking_finding_overrides_model_approval(tmp_path) -> None:
    project = _PolicyProject([{
        "description": "e2e policy: frontend change with no spec change",
        "file": "frontend/src/x.tsx", "blocking": True,
    }])
    eng = _engine(tmp_path, project)
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": [], **REVIEW_PROOF}))
    assert out["outcome"] == "review_rejected_fix_cycle"  # the gate, not the model, decides
    task = eng.store.load_task("r1", "t1")
    assert "e2e policy" in task.learnings[-1]
    # the hook received the intake worktree from the context plane
    assert project.seen_worktrees and project.seen_worktrees[0] == "/wt/42"
    events = [e for e in eng.store.read_events("r1") if e["type"] == "policy_findings_merged"]
    assert events and events[0]["blocking"] == 1
    # severity defaulted to critical: a repeated policy finding must never
    # convergence-auto-approve past the gate
    rec_issues = task.context.get("issues") or []
    assert any("critical" in str(i) for i in rec_issues)


def test_advisory_finding_becomes_tracked_followup(tmp_path) -> None:
    project = _PolicyProject([{
        "description": "API contract: frontend api types touched — double-check the lambda models",
        "blocking": False,
    }])
    eng = _engine(tmp_path, project)
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": [], **REVIEW_PROOF}))
    assert out["outcome"] == "task_completed"  # advisory never blocks
    filed = project.task_source.followups
    assert any("API contract" in f["title"] for f in filed)  # filed at finalize
    review = eng.store.load_task("r1", "t1").stages[Stage.REVIEW].output
    assert review["non_blocking"][0]["disposition"] == "file"


def test_raising_hook_never_breaks_record(tmp_path) -> None:
    project = _PolicyProject(RuntimeError("policy hook exploded"))
    eng = _engine(tmp_path, project)
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": [], **REVIEW_PROOF}))
    assert out["outcome"] == "task_completed"  # best-effort: review proceeds
    events = [e for e in eng.store.read_events("r1") if e["type"] == "policy_findings_failed"]
    assert events and "exploded" in events[0]["error"]


def test_no_hook_and_empty_findings_are_untouched(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)  # plain FakeProject: no review_findings hook
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": [], **REVIEW_PROOF}))
    assert out["outcome"] == "task_completed"


# --- #591: only what the task caused blocks ---------------------------------------------

_INTAKE = {"branch": "task/t1", "worktree": "/wt/42", "baseline_captured": True}
_APPROVE = {"approved": True, "issues": [], **REVIEW_PROOF}


def _red_lint(output: str) -> dict:
    return {"description": "ruff gate is RED", "severity": "critical", "blocking": True,
            "gate_leg": "ruff", "gate_output": output}


def _review_with_baseline(tmp_path, findings, baseline):
    project = _PolicyProject(findings)
    eng = _engine(tmp_path, project)
    w = _advance_to_review(eng, intake={**_INTAKE, "baseline_lint": baseline})
    return project, eng, eng.record("r1", make_result(w, structured_output=dict(_APPROVE)))


def test_lint_error_present_at_base_does_not_block(tmp_path) -> None:
    project, eng, out = _review_with_baseline(
        tmp_path, [_red_lint("a.py:7:1: E722 bare\nFound 1 error.\n")], ["a.py: E722 bare"]
    )
    assert out["outcome"] == "task_completed"
    review = eng.store.load_task("r1", "t1").stages[Stage.REVIEW].output
    assert review["approved"] is True and not review["issues"]
    (note,) = review["non_blocking"]
    assert note["disposition"] == "drop" and "already at base" in note["title"]
    # One inherited error must not open one identical issue per task in a batch.
    assert project.task_source.followups == []


def test_new_lint_error_blocks_and_names_only_the_new_one(tmp_path) -> None:
    _, eng, out = _review_with_baseline(
        tmp_path,
        [_red_lint("a.py:7:1: E722 bare\nb.py:1:1: F401 unused\n")],
        ["a.py: E722 bare"],
    )
    assert out["outcome"] == "review_rejected_fix_cycle"
    learning = eng.store.load_task("r1", "t1").learnings[-1]
    assert "F401" in learning and "E722" not in learning


def test_a_duplicate_of_an_inherited_error_blocks(tmp_path) -> None:
    _, _, out = _review_with_baseline(
        tmp_path, [_red_lint("a.py:1:1: E722 bare\na.py:9:1: E722 bare\n")], ["a.py: E722 bare"]
    )
    assert out["outcome"] == "review_rejected_fix_cycle"


def test_lint_finding_without_a_baseline_blocks_as_before(tmp_path) -> None:
    project = _PolicyProject([_red_lint("a.py:7:1: E722 bare\n")])
    eng = _engine(tmp_path, project)
    w = _advance_to_review(eng, intake=_INTAKE)
    out = eng.record("r1", make_result(w, structured_output=dict(_APPROVE)))
    assert out["outcome"] == "review_rejected_fix_cycle"


def test_gate_keys_never_reach_the_review_issues(tmp_path) -> None:
    _, eng, _ = _review_with_baseline(
        tmp_path,
        [{**_red_lint("b.py:1:1: F401 unused\n"), "disposition": "drop"}],
        ["a.py: E722 bare"],
    )
    issues = eng.store.load_task("r1", "t1").context["issues"]
    assert "ruff gate is RED" in str(issues)
    assert not {"gate_output", "gate_leg", "disposition"} & set(str(issues).replace("'", " ").split())


def test_advisory_title_is_the_first_line_of_a_multi_line_description(tmp_path) -> None:
    _, eng, _ = _review_with_baseline(
        tmp_path, [_red_lint("a.py:7:1: E722 bare\n")], ["a.py: E722 bare"]
    )
    (note,) = eng.store.load_task("r1", "t1").stages[Stage.REVIEW].output["non_blocking"]
    assert note["title"] == "ruff gate: 1 finding(s) already at base did not block."


def test_gate_record_is_evented_and_is_not_a_finding(tmp_path) -> None:
    record = {"leg": "benchmark", "verdict": "passed", "cpu_count": 8,
              "runs": [{"rc": 0, "duration_s": 1.0, "load_start": 1.0, "load_end": 1.2}]}
    project = _PolicyProject([{"gate_record": record}])
    eng = _engine(tmp_path, project)
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output=dict(_APPROVE)))
    assert out["outcome"] == "task_completed"
    (event,) = [e for e in eng.store.read_events("r1") if e["type"] == "policy_gate_result"]
    assert event["verdict"] == "passed" and event["cpu_count"] == 8
    assert event["runs"][0]["load_start"] == 1.0 and event["task_id"] == "t1"
    assert not [e for e in eng.store.read_events("r1") if e["type"] == "policy_findings_merged"]


def test_inconclusive_benchmark_completes_without_filing(tmp_path) -> None:
    project = _PolicyProject([{"description": "benchmark gate inconclusive: red, then green.",
                               "severity": "important", "blocking": False,
                               "disposition": "drop"}])
    eng = _engine(tmp_path, project)
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output=dict(_APPROVE)))
    assert out["outcome"] == "task_completed"
    assert project.task_source.followups == []


def test_baseline_lint_persists_on_the_task_doc(tmp_path) -> None:
    eng = _engine(tmp_path, FakeProject())
    _advance_to_review(eng, intake={**_INTAKE, "baseline_lint": ["a.py: E722 bare"]})
    # Re-read through a NEW engine: every CLI subcommand rebuilds it from defaults.
    fresh = _engine(tmp_path, FakeProject())
    task = fresh.store.load_task("r1", "t1")
    assert task.baseline_lint == ["a.py: E722 bare"]
    assert "baseline_lint" not in task.context  # 40-item cap, and fed to every prompt


def test_baseline_lint_past_the_cap_is_reported(tmp_path) -> None:
    eng = _engine(tmp_path, FakeProject())
    entries = [f"a.py: E{i}" for i in range(_MAX_BASELINE_LINT + 3)]
    _advance_to_review(eng, intake={**_INTAKE, "baseline_lint": entries})
    assert len(eng.store.load_task("r1", "t1").baseline_lint) == _MAX_BASELINE_LINT
    (event,) = [e for e in eng.store.read_events("r1")
                if e["type"] == "context_value_truncated" and e.get("field") == "baseline_lint"]
    assert event["dropped_items"] == 3


def test_a_model_lane_intake_cannot_excuse_its_own_errors() -> None:
    def result(mode, provider):
        return StageResult(
            work_item_id="w", content_hash="h", run_id="r", task_id="t", stage=Stage.INTAKE,
            attempt=0, model="m", status=ResultStatus.SUCCESS,
            structured_output={"baseline_lint": ["a.py: E722 bare", 7]},
            lane_used=LaneUsed(execution_mode=mode, provider=provider, invocation="x"),
            token_usage=TokenUsage(), completed_at="2026-10-14T00:00:00Z",
        )

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    _absorb_outputs(task, result(ExecutionMode.HEADLESS, Provider.CLAUDE))
    assert task.baseline_lint == []
    _absorb_outputs(task, result(ExecutionMode.ENGINE, Provider.NONE))
    assert task.baseline_lint == ["a.py: E722 bare"]  # and a non-string entry is dropped


# --- #634: the hook learns the task's changed files ---------------------------------------


def _git_worktree(path) -> str:
    """A real repo with one commit (returned as base_sha) and then a tools-only change:
    one modified tracked file and one untracked new file."""
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    path.mkdir()
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (path / "tools").mkdir()
    (path / "tools" / "report.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    base_sha = git("rev-parse", "HEAD")
    (path / "tools" / "report.py").write_text("x = 2\n")
    (path / "tools" / "new.py").write_text("y = 1\n")
    return base_sha


class _ReachProject(FakeProject):
    """A hook that takes ``changed_files`` and hands it to the shared benchmark gate."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list = []

    def review_findings(self, *, worktree=None, changed_files=None):
        from orchestrator.policy_gate import run_bench_gate

        self.seen.append(changed_files)

        def never(argv, cwd):  # noqa: ANN001, ARG001
            raise AssertionError("an unreachable benchmark must not run")

        return run_bench_gate(["bench"], worktree, never, changed_files=changed_files,
                              bench_paths=("src/taker",))


def test_hook_that_accepts_changed_files_gets_them_and_skips_the_benchmark(tmp_path) -> None:
    repo = tmp_path / "repo"
    base_sha = _git_worktree(repo)
    project = _ReachProject()
    eng = _engine(tmp_path / "store", project)
    w = _advance_to_review(eng, intake={**_INTAKE, "worktree": str(repo), "base_sha": base_sha})
    out = eng.record("r1", make_result(w, structured_output=dict(_APPROVE)))
    assert out["outcome"] == "task_completed"
    assert [sorted(c) for c in project.seen] == [["tools/new.py", "tools/report.py"]]
    (event,) = [e for e in eng.store.read_events("r1") if e["type"] == "policy_gate_result"]
    assert event["verdict"] == "skipped" and event["runs"] == []
    assert event["changed_files_count"] == 2 and event["bench_paths"] == ["src/taker"]
    assert project.task_source.followups == []


def test_changed_files_is_none_when_the_diff_cannot_be_read(tmp_path) -> None:
    """No base_sha (and a worktree that does not exist): the hook is told it cannot know,
    so the benchmark would run as before."""
    project = _ReachProject()
    project.review_findings = lambda *, worktree=None, changed_files=None: (  # type: ignore[method-assign]
        project.seen.append(changed_files) or [])
    eng = _engine(tmp_path, project)
    w = _advance_to_review(eng, intake=_INTAKE)
    eng.record("r1", make_result(w, structured_output=dict(_APPROVE)))
    assert project.seen == [None]


def test_legacy_hook_is_not_passed_changed_files(tmp_path) -> None:
    """A ``review_findings(*, worktree)`` hook written before #634 keeps working."""
    project = _PolicyProject([])
    eng = _engine(tmp_path, project)
    w = _advance_to_review(eng, intake=_INTAKE)
    out = eng.record("r1", make_result(w, structured_output=dict(_APPROVE)))
    assert out["outcome"] == "task_completed"
    assert project.seen_worktrees == ["/wt/42"]
    assert not [e for e in eng.store.read_events("r1") if e["type"] == "policy_findings_failed"]
