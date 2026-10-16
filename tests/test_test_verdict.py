"""A model-lane TEST stage's verdict comes from the project's declared test commands.

Regression for prediction run ``batch-s-420-233-421-422-423``: the TEST model ran the suite
serially instead of the declared parallel command, timed out twice on green code, and the
task failed. ``verify_test_verdict`` re-runs the declared commands after the model: a
claimed green must survive them, and a timeout over a clean worktree is settled by them.
"""

from __future__ import annotations

import subprocess

from adapters.execution.codex import CodexRunner
from adapters.execution.deterministic_test import DeterministicTestRunner
from adapters.execution.headless_claude import HeadlessClaudeRunner
from adapters.execution.test_verdict import verify_test_verdict
from adapters.execution.transport import RawResult
from orchestrator.failure_classifier import Failure
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.stages import render_prompt

_HEADLESS = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)


class _Classifier:
    def classify(self, test_output: str) -> list[Failure]:
        return [Failure(test=line.split(" ", 1)[1], kind="unit")
                for line in test_output.splitlines() if line.startswith("FAILED ")]


class _Proj:
    def __init__(self, unit: list[str]) -> None:
        self._unit = unit
        self.classifier = _Classifier()

    def test_unit_cmd(self, files=None):
        return self._unit

    def test_e2e_cmd(self, files=None):
        return None

    def test_shell_cmd(self, files=None):
        return None


_GREEN = _Proj(["sh", "-c", "exit 0"])
_RED = _Proj(["sh", "-c", "echo 'FAILED tests/t.py::test_x'; exit 1"])


def _repo(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    tmp_path.mkdir(parents=True, exist_ok=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    return tmp_path


def _wi(cwd, stage=Stage.TEST, checkpoint_tag=None) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="#42", stage=stage, prompt="p", schema_ref="test",
        model="sonnet", lane_policy=_HEADLESS, created_at="now", cwd=str(cwd),
        context={"baseline_failures": []}, checkpoint_tag=checkpoint_tag,
    )


def _model_green() -> RawResult:
    return RawResult({"passed": True, "failures": [], "tests_meaningful": True,
                      "validation_notes": "mutation check failed the new test"},
                     session_ref="sess-1")


def _timed_out() -> RawResult:
    return RawResult(None, exit_code=124, error="timed out after 1200s", session_ref="sess-1")


def test_a_claimed_green_that_the_declared_commands_fail_becomes_a_failure(tmp_path) -> None:
    raw, status = verify_test_verdict(_wi(_repo(tmp_path)), _model_green(),
                                      ResultStatus.SUCCESS, _RED)
    assert status is ResultStatus.FAILURE
    assert raw.structured_output["passed"] is False
    assert raw.structured_output["failures"] == ["tests/t.py::test_x"]
    assert raw.error.startswith("engine test verification:")
    assert raw.checkpoint is None
    assert raw.session_ref == "sess-1"  # the model's call is still the one recorded
    assert any(n["notice"] == "test_verdict_overridden" for n in raw.execution_notices)


def test_a_claimed_green_the_declared_commands_confirm_keeps_the_models_judgment(tmp_path) -> None:
    raw, status = verify_test_verdict(_wi(_repo(tmp_path)), _model_green(),
                                      ResultStatus.SUCCESS, _GREEN)
    assert status is ResultStatus.SUCCESS
    out = raw.structured_output
    assert out["passed"] is True and out["failures"] == []
    assert out["tests_meaningful"] is True  # meaningfulness stays the model's call
    assert "mutation check failed the new test" in out["validation_notes"]
    assert "Engine re-ran the project's declared test commands" in out["validation_notes"]
    assert "tests_meaningful not judged" not in out["validation_notes"]
    assert out["test_scope"] == "full"


def test_a_timeout_over_a_clean_worktree_is_settled_green_by_the_declared_commands(tmp_path) -> None:
    repo = _repo(tmp_path)
    raw, status = verify_test_verdict(_wi(repo, checkpoint_tag="ckpt-test"), _timed_out(),
                                      ResultStatus.TIMEOUT, _GREEN)
    assert status is ResultStatus.SUCCESS
    assert raw.exit_code == 0 and raw.error is None
    assert raw.structured_output["passed"] is True
    assert raw.structured_output["tests_meaningful"] is None  # not judged; REVIEW judges
    assert "timed out" in raw.structured_output["validation_notes"]
    assert raw.checkpoint and raw.checkpoint["tag"] == "ckpt-test"
    assert any(n["notice"] == "test_timeout_settled_by_declared_commands"
               for n in raw.execution_notices)


def test_a_timeout_over_a_dirty_worktree_stays_a_timeout(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("x = 2\n")  # the model died mid-edit
    timed_out = _timed_out()
    raw, status = verify_test_verdict(_wi(repo), timed_out, ResultStatus.TIMEOUT, _GREEN)
    assert status is ResultStatus.TIMEOUT and raw is timed_out


def test_a_timeout_the_declared_commands_fail_stays_a_timeout(tmp_path) -> None:
    timed_out = _timed_out()
    raw, status = verify_test_verdict(_wi(_repo(tmp_path)), timed_out,
                                      ResultStatus.TIMEOUT, _RED)
    assert status is ResultStatus.TIMEOUT and raw is timed_out


def test_other_stages_and_statuses_pass_through(tmp_path) -> None:
    repo = _repo(tmp_path)
    green = _model_green()
    assert verify_test_verdict(_wi(repo, stage=Stage.IMPLEMENT), green,
                               ResultStatus.SUCCESS, _RED) == (green, ResultStatus.SUCCESS)
    failed = RawResult(None, exit_code=1, error="boom")
    assert verify_test_verdict(_wi(repo), failed, ResultStatus.FAILURE, _GREEN) == (
        failed, ResultStatus.FAILURE)
    assert verify_test_verdict(_wi(repo), green, ResultStatus.SUCCESS, None) == (
        green, ResultStatus.SUCCESS)


def test_the_headless_runner_applies_the_declared_verdict(tmp_path) -> None:
    repo = _repo(tmp_path)
    runner = HeadlessClaudeRunner(lambda work: _model_green(), review_project=_RED)
    res = runner.dispatch(_wi(repo))
    assert res.status is ResultStatus.FAILURE
    assert res.structured_output["failures"] == ["tests/t.py::test_x"]


def test_only_lanes_wired_to_a_project_claim_the_capability() -> None:
    assert HeadlessClaudeRunner(lambda w: None, review_project=_GREEN).capabilities()[0] \
        .verifies_test_verdict is True
    assert HeadlessClaudeRunner(lambda w: None).capabilities()[0].verifies_test_verdict is False
    codex = CodexRunner(lambda w: None, review_project=_GREEN).capabilities()
    assert codex[0].verifies_test_verdict is True


def test_the_test_prompt_says_who_decides_only_on_a_verifying_lane() -> None:
    args = dict(task_id="#1", title="t", body="b")
    verified = render_prompt(Stage.TEST, test_verdict_verified=True, **args)
    plain = render_prompt(Stage.TEST, **args)
    assert "## Who decides pass/fail" in verified
    assert "Do not run the whole suite yourself" in verified
    assert "## Who decides pass/fail" not in plain
    assert "## Who decides pass/fail" not in render_prompt(
        Stage.REVIEW, test_verdict_verified=True, **args)


def _flaky(marker) -> _Proj:
    """Fails on the first run (one named test), passes on every later run."""
    return _Proj(["sh", "-c", f"if [ -f {marker} ]; then exit 0; fi; touch {marker}; "
                              "echo 'FAILED tests/t.py::test_timing'; exit 1"])


def test_a_failure_that_passes_on_rerun_is_flaky_not_caused(tmp_path) -> None:
    repo = _repo(tmp_path / "repo")
    raw, status = verify_test_verdict(_wi(repo), _model_green(), ResultStatus.SUCCESS,
                                      _flaky(tmp_path / "ran"))
    assert status is ResultStatus.SUCCESS
    out = raw.structured_output
    assert out["passed"] is True and out["failures"] == []
    assert out["flaky_failures"] == ["tests/t.py::test_timing"]
    assert any(n["notice"] == "test_flaky_failures_cleared" for n in raw.execution_notices)


def test_a_failure_that_reproduces_on_rerun_stays_red(tmp_path) -> None:
    run = DeterministicTestRunner(_RED).evaluate(_wi(_repo(tmp_path)))
    assert run.ok is False and run.out["failures"] == ["tests/t.py::test_x"]
    assert "re-ran the unit command once; still red" in run.out["validation_notes"]
    assert "flaky_failures" not in run.out


def test_many_failures_are_not_rerun(tmp_path) -> None:
    lines = "; ".join(f"echo 'FAILED tests/t.py::test_{i}'" for i in range(6))
    marker = tmp_path / "ran"
    proj = _Proj(["sh", "-c", f"if [ -f {marker} ]; then exit 0; fi; touch {marker}; "
                              f"{lines}; exit 1"])
    run = DeterministicTestRunner(proj).evaluate(_wi(_repo(tmp_path / "repo")))
    assert run.ok is False and len(run.out["failures"]) == 6


def test_a_worktree_missing_on_disk_is_not_verified(tmp_path) -> None:
    green = _model_green()
    assert verify_test_verdict(_wi(tmp_path / "gone"), green, ResultStatus.SUCCESS, _RED) == (
        green, ResultStatus.SUCCESS)
