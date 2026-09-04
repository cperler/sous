"""#390: how much a REVIEW may trust a command it runs in its own workspace.

Six retrospectives across four runs (ff-v1-b6 #30/#32, ff-v1-b15 #14, ff-v1-b24 #145/#146)
report one failure mode: a review workspace whose environment was provisioned by COPY
resolves the package to a SIBLING worktree — a copied venv's launcher shebang, a stale
editable-install `.pth`, or (b24) an invocation whose rootdir picks a different install — so
a revert-and-rerun there can falsely pass, falsely fail, or disagree with itself run to run.

The structural half is #381/#391: a disposable REVIEW checkout rebuilds adapter-declared
artifacts and origin probes fail closed. Those hooks are OPTIONAL by contract, so this
covers the prompt half, which is what remains when a project declares none:

* the reviewer's own mutation check is framed as CORROBORATION of the TEST stage's result,
  in both failure directions, and an unattributable result is inconclusive rather than a
  `tests_meaningful: false` or a blocking issue;
* the single-reviewer prompt and the panel's `find:tests` finder say it from ONE constant,
  so the two cannot drift (the `_DOCS_ONLY_DIRECTIVE`/`_DESIGN_CRITERIA` precedent);
* a workspace whose origin nothing can verify says so IN THE PROMPT, not only in
  `events.jsonl` — and a project that does declare the hooks renders a byte-identical
  pre-#390 prompt.
"""

from __future__ import annotations

from adapters.execution.base import SUPPORTED, CapabilityDescriptor, Registry
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.routing import Router
from orchestrator.schemas.enums import ExecutionMode, Provider, Stage
from orchestrator.stages import (
    _MUTATION_CHECK_TRUST,
    _WORKTREE_ORIGIN_UNVERIFIED_DIRECTIVE,
    render_prompt,
    render_review_plan,
)
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

_UNVERIFIED_MARKER = "toolchain origin was NOT verified"


def _review_prompt(**kw) -> str:
    return render_prompt(Stage.REVIEW, task_id="t1", title="Fix the thing", body="do it", **kw)


# --- (a) the mutation-check trust posture ------------------------------------------------

def test_single_reviewer_is_told_its_mutation_check_only_corroborates() -> None:
    """The pre-#390 wording covered ONE direction (a mutation that fails to fail) and framed
    the check as decisive. Both halves are load-bearing: b24 #145 saw the same command pass
    and fail alternately."""
    prompt = _review_prompt()
    assert _MUTATION_CHECK_TRUST in prompt
    assert "CORROBORATES" in prompt
    assert "spurious failure" in prompt  # the false-FAILURE direction, not just false-pass
    assert "INCONCLUSIVE" in prompt


def test_inconclusive_check_may_not_drive_a_rejection() -> None:
    """The concrete harm: a sandbox that silently ran a sibling worktree's install talks the
    reviewer into `tests_meaningful: false` (a fix cycle) or a blocking issue."""
    assert "`tests_meaningful: false`" in _MUTATION_CHECK_TRUST
    assert "blocking issue" in _MUTATION_CHECK_TRUST


def test_origin_probe_must_match_how_the_suite_runs() -> None:
    """b24's root cause: `__file__` resolved correctly under `pytest tests/...` but pointed
    at the implement worktree from a probe rooted outside it."""
    assert "__file__" in _MUTATION_CHECK_TRUST
    assert "rootdir" in _MUTATION_CHECK_TRUST


def test_grounding_must_go_through_the_test_runner_not_python_c() -> None:
    """#502: `python -c` puts cwd first on `sys.path`, so it agrees with the workspace even
    when the runner imports a sibling worktree. Both places that ask the reviewer to ground a
    result must name the runner, or the reviewer can "confirm" it with the check that lies."""
    for directive in (_MUTATION_CHECK_TRUST, _WORKTREE_ORIGIN_UNVERIFIED_DIRECTIVE):
        assert "TEST RUNNER" in directive.upper()
        assert "python -c" in directive
        assert "sys.path" in directive


def test_panel_tests_finder_shares_the_same_constant() -> None:
    """A finder runs its revert check in its own disposable copy, so it faces the identical
    aliasing. One constant, so the two wordings cannot drift apart."""
    plan = render_review_plan(task_id="t1", title="Fix the thing", body="do it")
    tests_finder = next(f for f in plan.finders if f.lens == "find:tests")
    assert _MUTATION_CHECK_TRUST in tests_finder.prompt


# --- (b) stating an unverified workspace in-band ------------------------------------------

def test_unverified_workspace_is_stated_in_the_prompt() -> None:
    prompt = _review_prompt(worktree_origin_unverified=True)
    assert _WORKTREE_ORIGIN_UNVERIFIED_DIRECTIVE in prompt
    assert _UNVERIFIED_MARKER in prompt


def test_verified_workspace_prompt_is_byte_identical_to_pre_390() -> None:
    """Conditional block, default off (the #302 `tool_posture_unenforced` precedent): a
    project that declares the hooks pays no tokens for a caveat that does not apply."""
    assert _review_prompt(worktree_origin_unverified=False) == _review_prompt()
    assert _UNVERIFIED_MARKER not in _review_prompt()


def test_only_review_renders_the_unverified_block() -> None:
    """IMPLEMENT/TEST work in the task worktree the engine provisioned and installed; REVIEW
    is the stage handed a workspace whose toolchain nothing proved is its own."""
    for stage in (Stage.IMPLEMENT, Stage.TEST, Stage.SCOPE):
        rendered = render_prompt(
            stage, task_id="t1", title="t", body="b", worktree_origin_unverified=True
        )
        assert _UNVERIFIED_MARKER not in rendered


# --- (c) the engine resolves the flag, stages.py knows nothing about adapters --------------

class _OriginProject:
    """A project declaring the #391 hooks, wrapping the shared FakeProject."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def fresh_install_paths(self) -> list[str]:
        return [".venv"]

    def worktree_origin_probes(self) -> list[tuple[str, list[str], str]]:
        return [("interpreter", ["echo", "/x"], "launcher")]


def _origin_registry() -> Registry:
    """headless×claude declaring the REVIEW origin-preflight capability."""
    reg = Registry()
    for mode, provider, verifies in (
        (ExecutionMode.HEADLESS, Provider.CLAUDE, True),
        (ExecutionMode.ENGINE, Provider.NONE, False),
    ):
        reg.register_external(CapabilityDescriptor(
            execution_mode=mode, provider=provider, in_process=True,
            schema_enforced=True, verifies_worktree_origin=verifies, status=SUPPORTED,
        ))
    return reg


def _review_work(tmp_path, project):
    eng = Engine(
        StatusStore(tmp_path),
        CostLedger(tmp_path / "stage-costs.jsonl"),
        project,
        registry=_origin_registry(),
        router=Router(execution_mode=ExecutionMode.HEADLESS),
    )
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        if w.stage is Stage.REVIEW:
            return w
        eng.record("r1", make_result(w))
    raise AssertionError("task finished without reaching REVIEW")


def test_engine_states_the_caveat_when_the_adapter_declares_no_hooks(tmp_path, project) -> None:
    """The lane declares `verifies_worktree_origin`, but with nothing declared the probes
    verify NOTHING — so lane capability alone must not read as a verified workspace."""
    assert _UNVERIFIED_MARKER in _review_work(tmp_path, project).prompt


def test_engine_omits_the_caveat_when_the_adapter_declares_hooks(tmp_path, project) -> None:
    assert _UNVERIFIED_MARKER not in _review_work(tmp_path, _OriginProject(project)).prompt
