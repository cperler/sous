"""Deterministic TEST + DELIVER ENGINE-lane runners (#33): $0 shell test-run and branch
push, no model call. Unit-tests run real (fast) shell commands for TEST and an injected
runner for DELIVER, so nothing touches the network or a real git. Structured outputs are
validated against the canonical stage JSON schemas.

#389 moved the PR-opening half of DELIVER into its own PUBLISH stage, so its runner and
tests live in tests/test_publish_stage.py."""

from __future__ import annotations

from collections import namedtuple

from jsonschema import Draft202012Validator

from adapters.execution.deterministic_deliver import DeterministicDeliverRunner
from adapters.execution.deterministic_setup import DeterministicSetupRunner
from adapters.execution.deterministic_test import DeterministicTestRunner
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.failure_classifier import Failure
from orchestrator.schemas.enums import (
    ExecutionLane,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
)
from orchestrator.schemas.stage_schemas import load_stage_schema
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result

_ENGINE = LanePolicy(execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE, allow_fallback=False)


def _assert_schema_valid(ref: str, out: dict) -> None:
    Draft202012Validator(load_stage_schema(ref)).validate(out)


def _wi(stage: Stage, *, schema_ref: str, cwd=None, context=None, checkpoint_tag=None) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="#42", stage=stage, prompt="p",
        schema_ref=schema_ref, model="engine", lane_policy=_ENGINE, created_at="now",
        cwd=cwd, context=context, checkpoint_tag=checkpoint_tag,
    )


# --- TEST runner ----------------------------------------------------------
class _Classifier:
    """Parses 'FAILED <id>' lines into unit failures (test-controllable)."""

    def classify(self, test_output: str) -> list[Failure]:
        return [
            Failure(test=line.split(" ", 1)[1], kind="unit")
            for line in test_output.splitlines()
            if line.startswith("FAILED ")
        ]

    def impacted_tests(self, changed_files):
        return []


class _TestProj:
    def __init__(self, *, unit, e2e=None, shell=None, classifier=None):
        self._unit, self._e2e, self._shell = unit, e2e, shell
        self.classifier = classifier if classifier is not None else _Classifier()

    def test_unit_cmd(self, files=None):
        return self._unit

    def test_e2e_cmd(self, files=None):
        return self._e2e

    def test_shell_cmd(self, files=None):
        return self._shell


def test_test_green_run_succeeds_schema_valid(tmp_path) -> None:
    res = DeterministicTestRunner(_TestProj(unit=["sh", "-c", "exit 0"])).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={"baseline_failures": []})
    )
    assert res.status is ResultStatus.SUCCESS
    assert (res.lane_used.execution_mode, res.lane_used.provider) == (ExecutionMode.ENGINE, Provider.NONE)
    out = res.structured_output
    assert out["passed"] is True and out["failures"] == []
    # #261: a script cannot judge meaningfulness, so it makes NO claim (null) — never the
    # affirmative `true` it used to write next to a "not judged by this runner" note.
    assert out["tests_meaningful"] is None
    _assert_schema_valid("test", out)


def test_engine_lane_never_claims_tests_meaningful_on_any_exit_path(tmp_path) -> None:
    """#261 regression: the ENGINE-lane TEST runner must not emit an affirmative
    `tests_meaningful: true` on ANY of its three exit paths — it cannot judge meaningfulness
    (a `true` there is a false audit record; the lite lane's REVIEW then omitted the field
    and nobody judged it at all). The key stays PRESENT as null: an honest abstention that
    still satisfies the schema's `required` list. It must never be `false` either — that is
    the engine's veto over a judgment this runner did not make.
    """
    # (a) real suite run (green) and (b) real suite run (red)
    for cmd, red in ((["sh", "-c", "exit 0"], False),
                     (["sh", "-c", "echo 'FAILED a::t1'; exit 1"], True)):
        res = DeterministicTestRunner(_TestProj(unit=cmd)).dispatch(
            _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={})
        )
        out = res.structured_output
        assert (res.status is ResultStatus.FAILURE) is red
        assert out["tests_meaningful"] is None, cmd
        assert "not judged by this runner" in out["validation_notes"]
        _assert_schema_valid("test", out)

    # (c) no test commands defined (the ['true'] no-op sentinel → nothing to run)
    res = DeterministicTestRunner(_TestProj(unit=["true"])).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={})
    )
    assert res.status is ResultStatus.SUCCESS
    assert res.structured_output["tests_meaningful"] is None
    assert "no test commands defined" in res.structured_output["validation_notes"]
    _assert_schema_valid("test", res.structured_output)


def test_test_caused_failure_fails_with_classified_kind(tmp_path) -> None:
    proj = _TestProj(unit=["sh", "-c", "echo 'FAILED tests/test_x.py::t1'; exit 1"])
    res = DeterministicTestRunner(proj).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={"baseline_failures": []})
    )
    assert res.status is ResultStatus.FAILURE  # nonzero exit → engine's retry/fix loop
    out = res.structured_output
    assert out["passed"] is False and out["failures"] == ["tests/test_x.py::t1"]
    assert "unit" in (res.error or "")  # classified failure kind surfaced
    _assert_schema_valid("test", out)


def test_test_unparsed_failure_keeps_bounded_output_tail(tmp_path) -> None:
    # classifier parses nothing → honest caused red with a BOUNDED output tail in the notes.
    proj = _TestProj(
        unit=["sh", "-c", "python3 -c \"print('A'*6000 + 'ZZTAILMARK')\"; exit 2"],
        classifier=_Classifier(),  # no 'FAILED ' lines → no parsed failures
    )
    res = DeterministicTestRunner(proj).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={})
    )
    assert res.status is ResultStatus.FAILURE
    out = res.structured_output
    assert out["failures"] == ["<unit rc=2>"] and "unknown" in (res.error or "")
    assert "ZZTAILMARK" in out["validation_notes"]  # the TAIL survives
    assert "A" * 4001 not in out["validation_notes"]  # ... but the head is truncated (bounded)
    _assert_schema_valid("test", out)


def test_test_inherited_baseline_failure_is_not_counted(tmp_path) -> None:
    proj = _TestProj(unit=["sh", "-c", "echo 'FAILED a::t1'; exit 1"])
    res = DeterministicTestRunner(proj).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path),
            context={"baseline_failures": ["a::t1"]})
    )
    assert res.status is ResultStatus.SUCCESS  # RED at base → inherited, not this change's fault
    out = res.structured_output
    assert out["passed"] is True and out["failures"] == []
    assert "inherited" in out["validation_notes"]


def test_test_splits_inherited_from_caused(tmp_path) -> None:
    proj = _TestProj(unit=["sh", "-c", "printf 'FAILED a::t1\\nFAILED b::t2\\n'; exit 1"])
    res = DeterministicTestRunner(proj).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path),
            context={"baseline_failures": ["a::t1"]})
    )
    assert res.status is ResultStatus.FAILURE
    out = res.structured_output
    assert out["failures"] == ["b::t2"]  # only the NEW failure counts
    assert "inherited" in out["validation_notes"]


def test_test_skips_true_sentinel_e2e_and_shell(tmp_path) -> None:
    proj = _TestProj(unit=["sh", "-c", "exit 0"], e2e=["true"], shell=["true"])
    res = DeterministicTestRunner(proj).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={})
    )
    assert res.status is ResultStatus.SUCCESS
    notes = res.structured_output["validation_notes"]
    assert "e2e" not in notes and "shell" not in notes  # sentinel → not run


def test_test_runs_e2e_when_defined_and_reports_its_failure(tmp_path) -> None:
    proj = _TestProj(
        unit=["sh", "-c", "exit 0"],
        e2e=["sh", "-c", "echo 'FAILED e2e::t'; exit 1"],
    )
    res = DeterministicTestRunner(proj).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={})
    )
    assert res.status is ResultStatus.FAILURE
    assert res.structured_output["failures"] == ["e2e::t"]


def test_test_timeout_fails_as_infra(tmp_path) -> None:
    proj = _TestProj(unit=["sh", "-c", "sleep 3"])
    res = DeterministicTestRunner(proj, timeout_s=0.2).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={})
    )
    assert res.status is ResultStatus.FAILURE
    assert "infra" in (res.error or "") and "<unit timeout>" in res.structured_output["failures"]


def test_test_no_commands_passes_vacuously(tmp_path) -> None:
    proj = _TestProj(unit=["true"])  # the no-op sentinel: nothing to run
    res = DeterministicTestRunner(proj).dispatch(
        _wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path), context={})
    )
    assert res.status is ResultStatus.SUCCESS
    out = res.structured_output
    assert out["passed"] is True and "no test commands" in out["validation_notes"]
    _assert_schema_valid("test", out)


# --- DELIVER runner (injected git) ---------------------------------------
# #389: DELIVER pushes the branch and nothing else. The PR-opening tests moved with the
# work, to tests/test_publish_stage.py.
_CP = namedtuple("CP", ["returncode", "stdout", "stderr"])

_HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
_OLD_HEAD = "0f1e2d3c4b5a69788796a5b4c3d2e1f012345678"


def _cp(rc: int = 0, out: str = "", err: str = "") -> _CP:
    return _CP(rc, out, err)


class _FakeGh:
    """Records (argv, cwd) and answers by argv shape — no real git/gh/network."""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv, cwd=None, **_kw):
        self.calls.append((list(argv), cwd))
        return self.responder(list(argv))

    def ran(self, *needles: str) -> bool:
        return any(all(n in " ".join(argv) for n in needles) for argv, _ in self.calls)


def _deliver_wi(cwd="/wt/42", context=None) -> WorkItem:
    ctx = {"task_id": "#42", "title": "Fix the bug", "issue_number": 42}
    if context is not None:
        ctx = {**ctx, **context}
    return _wi(Stage.DELIVER, schema_ref="deliver", cwd=cwd, context=ctx)


def _push_responder(*, remote_before: str = "", remote_after: str = _HEAD, head: str = _HEAD):
    """A git double whose remote holds ``remote_before`` until a push, then ``remote_after``."""
    state = {"remote": remote_before}

    def responder(argv):
        if argv[:2] == ["git", "rev-parse"] and "--abbrev-ref" in argv:
            return _cp(0, "task/42\n")
        if argv[:2] == ["git", "rev-parse"]:
            return _cp(0, f"{head}\n")
        if "rev-list" in argv:
            return _cp(0, "2\n")
        if "ls-remote" in argv:
            return _cp(0, f"{state['remote']}\trefs/heads/task/42\n" if state["remote"] else "")
        if "push" in argv:
            state["remote"] = remote_after
            return _cp(0)
        return _cp(0)

    return responder


def test_deliver_pushes_the_branch_and_reports_the_landed_head(tmp_path) -> None:
    gh = _FakeGh(_push_responder())
    res = DeterministicDeliverRunner(FakeProject(), runner=gh).dispatch(_deliver_wi())

    assert res.status is ResultStatus.SUCCESS
    out = res.structured_output
    assert out == {"branch": "task/42", "pushed_head_sha": _HEAD}
    _assert_schema_valid("deliver", out)
    assert gh.ran("push", "origin", "task/42")
    # DELIVER must not open, look up, or comment on a PR any more — that is PUBLISH's job,
    # and it runs only after REVIEW approves.
    assert not any(argv[:1] == ["gh"] for argv, _ in gh.calls)


def test_deliver_verifies_the_push_landed_rather_than_trusting_exit_zero(tmp_path) -> None:
    # `git push` exiting 0 without moving the remote ref (a hook, a stale lock) must not be
    # reported as a delivery: the whole durability property is that the head is RECOVERABLE.
    gh = _FakeGh(_push_responder(remote_after=_OLD_HEAD))
    res = DeterministicDeliverRunner(FakeProject(), runner=gh).dispatch(_deliver_wi())

    assert res.status is ResultStatus.FAILURE
    assert "did not land" in (res.error or "")


def test_deliver_is_a_noop_success_when_the_remote_already_has_the_head(tmp_path) -> None:
    # The fix-cycle shape after #389: a re-deliver whose commits are already on the remote
    # is an ordinary no-op, not the old "reuse the existing PR" special case.
    gh = _FakeGh(_push_responder(remote_before=_HEAD))
    res = DeterministicDeliverRunner(FakeProject(), runner=gh).dispatch(_deliver_wi())

    assert res.status is ResultStatus.SUCCESS
    assert res.structured_output == {"branch": "task/42", "pushed_head_sha": _HEAD}
    assert not gh.ran("push")


def test_deliver_refuses_a_branch_with_no_commits(tmp_path) -> None:
    def responder(argv):
        if argv[:2] == ["git", "rev-parse"] and "--abbrev-ref" in argv:
            return _cp(0, "task/42\n")
        if "rev-list" in argv:
            return _cp(0, "0\n")
        return _cp(0)

    gh = _FakeGh(responder)
    res = DeterministicDeliverRunner(FakeProject(), runner=gh).dispatch(_deliver_wi())
    assert res.status is ResultStatus.FAILURE and "no commits" in (res.error or "")
    assert not gh.ran("push")


def test_deliver_fails_on_push_error(tmp_path) -> None:
    def responder(argv):
        if argv[:2] == ["git", "rev-parse"] and "--abbrev-ref" in argv:
            return _cp(0, "task/42\n")
        if argv[:2] == ["git", "rev-parse"]:
            return _cp(0, f"{_HEAD}\n")
        if "rev-list" in argv:
            return _cp(0, "2\n")
        if "ls-remote" in argv:
            return _cp(0, "")
        if "push" in argv:
            return _cp(1, "", "remote rejected")
        return _cp(0)

    res = DeterministicDeliverRunner(FakeProject(), runner=_FakeGh(responder)).dispatch(_deliver_wi())
    assert res.status is ResultStatus.FAILURE and "push failed" in (res.error or "")


def test_deliver_fails_when_head_is_unresolvable(tmp_path) -> None:
    def responder(argv):
        if argv[:2] == ["git", "rev-parse"] and "--abbrev-ref" in argv:
            return _cp(0, "task/42\n")
        if argv[:2] == ["git", "rev-parse"]:
            return _cp(1, "", "bad revision")
        if "rev-list" in argv:
            return _cp(0, "2\n")
        return _cp(0)

    res = DeterministicDeliverRunner(FakeProject(), runner=_FakeGh(responder)).dispatch(_deliver_wi())
    assert res.status is ResultStatus.FAILURE and "HEAD" in (res.error or "")


def test_deliver_dispatch_never_escapes_a_raising_runner(tmp_path) -> None:
    def boom(argv, cwd=None, **_kw):
        raise RuntimeError("git exploded")

    res = DeterministicDeliverRunner(FakeProject(), runner=boom).dispatch(_deliver_wi())
    assert res.status is ResultStatus.FAILURE and "exploded" in (res.error or "")


def test_deliver_requires_worktree_cwd(tmp_path) -> None:
    res = DeterministicDeliverRunner(FakeProject(), runner=_FakeGh(lambda a: _cp(0))).dispatch(
        _deliver_wi(cwd=None)
    )
    assert res.status is ResultStatus.FAILURE and "worktree" in (res.error or "")


# --- wiring: the ENGINE cell serves all three, engine routes opt-ins there ----
def test_engine_runner_delegates_test_deliver_and_publish(tmp_path) -> None:
    # The single (ENGINE, NONE) runner dispatches TEST/DELIVER/PUBLISH by stage (no separate
    # cell per stage).
    setup = DeterministicSetupRunner(_TestProj(unit=["sh", "-c", "exit 0"]))
    test_res = setup.dispatch(_wi(Stage.TEST, schema_ref="test", cwd=str(tmp_path),
                                  context={"baseline_failures": []}))
    assert test_res.status is ResultStatus.SUCCESS and test_res.structured_output["passed"] is True
    # DELIVER with no cwd fails BEFORE any subprocess — proves delegation reaches the deliver
    # runner without touching git/gh.
    deliver_res = setup.dispatch(_wi(Stage.DELIVER, schema_ref="deliver", cwd=None, context={}))
    assert deliver_res.status is ResultStatus.FAILURE and "worktree" in (deliver_res.error or "")
    # Same proof for PUBLISH (#389): no cwd fails before any subprocess.
    publish_res = setup.dispatch(_wi(Stage.PUBLISH, schema_ref="publish", cwd=None, context={}))
    assert publish_res.status is ResultStatus.FAILURE and "worktree" in (publish_res.error or "")


def test_engine_routes_opted_in_stages_to_engine_lane(tmp_path) -> None:
    eng = Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), FakeProject())
    eng.create_run("r")
    task = eng.add_task("r", "t1", deterministic_stages=[Stage.TEST, Stage.DELIVER])
    assert task.deterministic_stages == (Stage.TEST, Stage.DELIVER)  # persisted

    lanes: dict[Stage, ExecutionMode] = {}
    ctx_seen: dict[Stage, dict | None] = {}
    while (w := eng.next_work("r", "t1")) is not None:
        lanes[w.stage] = w.lane_policy.execution_mode
        ctx_seen[w.stage] = w.context
        eng.record("r", make_result(w))

    # intake is always deterministic; the opted-in TEST/DELIVER now route to the ENGINE lane.
    assert lanes[Stage.INTAKE] is ExecutionMode.ENGINE
    assert lanes[Stage.TEST] is ExecutionMode.ENGINE
    assert lanes[Stage.DELIVER] is ExecutionMode.ENGINE
    # the un-opted model stages are untouched (default interactive lane).
    assert lanes[Stage.SCOPE] is ExecutionMode.INTERACTIVE
    assert lanes[Stage.IMPLEMENT] is ExecutionMode.INTERACTIVE
    assert lanes[Stage.REVIEW] is ExecutionMode.INTERACTIVE
    # deterministic stages carry structured context; model stages do not.
    assert ctx_seen[Stage.DELIVER] is not None and ctx_seen[Stage.DELIVER]["task_id"] == "t1"
    assert ctx_seen[Stage.SCOPE] is None


# --- #68: micro/lite presets default to deterministic TEST/DELIVER --------------
def _eng(tmp_path) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), FakeProject())


def test_lite_preset_defaults_to_deterministic_test_and_deliver(tmp_path) -> None:
    eng = _eng(tmp_path)
    eng.create_run("r", ExecutionLane.LITE)
    task = eng.add_task("r", "t1")  # no explicit deterministic_stages
    assert task.deterministic_stages == (Stage.TEST, Stage.DELIVER)


def test_micro_preset_defaults_to_deterministic_deliver_only(tmp_path) -> None:
    # MICRO has no TEST stage, so the default is DELIVER only (intersected with the pipeline).
    eng = _eng(tmp_path)
    eng.create_run("r", ExecutionLane.MICRO)
    task = eng.add_task("r", "t1")
    assert task.deterministic_stages == (Stage.DELIVER,)
    assert Stage.TEST not in task.pipeline


def test_full_preset_keeps_model_test_and_deliver(tmp_path) -> None:
    eng = _eng(tmp_path)
    eng.create_run("r", ExecutionLane.FULL)
    task = eng.add_task("r", "t1")
    assert task.deterministic_stages == ()  # FULL pays for model TEST/DELIVER


def test_explicit_deterministic_stages_override_the_preset_default(tmp_path) -> None:
    eng = _eng(tmp_path)
    eng.create_run("r", ExecutionLane.LITE)
    task = eng.add_task("r", "t1", deterministic_stages=[Stage.DELIVER])
    assert task.deterministic_stages == (Stage.DELIVER,)  # caller wins, TEST stays model


def test_explicit_pipeline_pin_opts_out_of_the_preset_default(tmp_path) -> None:
    # A bespoke pipeline states its own deterministic stages; the lane preset default
    # does not silently reach it.
    eng = _eng(tmp_path)
    eng.create_run("r", ExecutionLane.LITE)
    task = eng.add_task("r", "t1", pipeline=[Stage.IMPLEMENT, Stage.TEST, Stage.DELIVER])
    assert task.deterministic_stages == ()
