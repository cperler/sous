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

#411 is the same cluster recurring twenty-odd more times across ten runs, which is the
evidence that the prompt half above did not hold on its own. Two changes, covered at the
bottom of this file:

* the origin check LEADS the unverified directive as an ordering ("before any command whose
  result you will rely on"), because ff-batch-20260903-1724 #232 skipped it exactly as a
  trailing conditional invites — and the recipe itself now lives in ONE constant that both
  directives compose, rather than being restated (and drifting) per site;
* the reviewer REPORTS `workspace_origin`, and anything short of `confirmed` — including an
  omission — becomes a warning-grade `review_workspace_origin_unconfirmed` event. Report-only:
  a reviewer who ran nothing dynamic can still review a diff correctly, so the fix is to stop
  letting "unconfirmed" and "confirmed" look alike, not to start rejecting.
"""

from __future__ import annotations

from adapters.execution.base import SUPPORTED, CapabilityDescriptor, Registry
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.routing import Router
from orchestrator.schemas.enums import ExecutionMode, Provider, Stage
from orchestrator.stages import (
    _MUTATION_CHECK_TRUST,
    _RUNNER_ORIGIN_CHECK,
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


# --- (d) #411: the check is an ORDERING, and its outcome is reported ----------------------

def test_the_origin_check_is_stated_once_and_composed_by_both_directives() -> None:
    """#411's subtractive half. The recipe was spelled out twice at length, and each copy is a
    place the wording can drift from the invocation that actually proves the point — which is
    how the pre-#502 `python -c` recipe survived in one directive after the other moved on."""
    assert _RUNNER_ORIGIN_CHECK in _MUTATION_CHECK_TRUST
    assert _RUNNER_ORIGIN_CHECK in _WORKTREE_ORIGIN_UNVERIFIED_DIRECTIVE


def test_unverified_workspace_orders_the_check_before_any_relied_on_command() -> None:
    """ff-batch-20260903-1724 #232: the reviewer DID have the probe available and caught a real
    sibling-worktree mismatch with it — but only by chance, because #390 framed it as a
    fallback for a result that already looks wrong ("easy to skip on a run where nothing seems
    anomalous"). The check has to lead."""
    directive = _WORKTREE_ORIGIN_UNVERIFIED_DIRECTIVE
    assert "Do this FIRST" in directive
    assert "not after one looks suspicious" in directive
    # ...and it precedes the fall-back-to-reading-the-diff exit, rather than hiding behind it.
    assert directive.index("Do this FIRST") < directive.index("prefer reading the diff")


def test_unverified_workspace_names_the_remedies_live_runs_proved() -> None:
    """A mismatch used to dead-end at "prefer reading the diff". Two escapes were found the
    hard way: module invocation past the console-script shim (ff-v1-b32 #258, ff-v1-b21 #100)
    and corroborating in the tree the runner really reads when it is provably identical
    (ff-batch-20260903-2004 #249)."""
    directive = _WORKTREE_ORIGIN_UNVERIFIED_DIRECTIVE
    assert "python -m <runner>" in directive
    assert "console-script shim" in directive
    assert "identical commit" in directive


def test_unverified_workspace_asks_for_the_outcome_and_denies_a_silent_omission() -> None:
    directive = _WORKTREE_ORIGIN_UNVERIFIED_DIRECTIVE
    assert "`workspace_origin`" in directive
    for value in ("`confirmed`", "`mismatched`", "`not_checked`"):
        assert value in directive
    assert "does not read as confirmed" in directive


def test_verified_workspace_is_never_asked_for_workspace_origin() -> None:
    """The field belongs to the conditional block, so the byte-identity guarantee above still
    holds: a project that proves origin structurally pays no tokens for the reporting ask."""
    assert "workspace_origin" not in _review_prompt()


def _review_events(tmp_path, project, structured_output):
    work = _review_work(tmp_path, project)
    eng = Engine(
        StatusStore(tmp_path),
        CostLedger(tmp_path / "stage-costs.jsonl"),
        project,
        registry=_origin_registry(),
        router=Router(execution_mode=ExecutionMode.HEADLESS),
    )
    eng.record("r1", make_result(work, structured_output=structured_output))
    return [
        e for e in StatusStore(tmp_path).read_events("r1")
        if e["type"] == "review_workspace_origin_unconfirmed"
    ]


def _approved(**extra):
    return {"approved": True, "issues": [], "tests_meaningful": True, **extra}


def test_omitted_workspace_origin_events_on_an_unverified_review(tmp_path, project) -> None:
    """The #390 failure mode made structural: an unverified REVIEW that never grounded its
    commands leaves a warning in `events.jsonl` instead of an indistinguishable silence."""
    (event,) = _review_events(tmp_path, project, _approved())
    assert event["level"] == "warning"
    assert event["kind"] == "not_checked"
    assert event["reported"] is None


def test_mismatched_workspace_origin_events_with_its_own_kind(tmp_path, project) -> None:
    """A reviewer that LOOKED and found a sibling worktree is a different, louder fact than
    one that never looked; the audit must be able to tell them apart."""
    (event,) = _review_events(tmp_path, project, _approved(workspace_origin="mismatched"))
    assert event["kind"] == "mismatched"


def test_confirmed_workspace_origin_is_silent(tmp_path, project) -> None:
    assert _review_events(tmp_path, project, _approved(workspace_origin="confirmed")) == []


def test_a_project_declaring_the_hooks_is_never_evented(tmp_path, project) -> None:
    """It proves origin structurally and is never asked, so an absent field is not a miss —
    the record-time predicate must be the SAME one the dispatch used."""
    assert _review_events(tmp_path, _OriginProject(project), _approved()) == []


def test_the_notice_never_blocks_completion(tmp_path, project) -> None:
    """Report-only, deliberately: a reviewer who ran no dynamic check can still review a diff
    correctly, and #390's lesson was that the omission must be LOUD, not fatal."""
    work = _review_work(tmp_path, project)
    eng = Engine(
        StatusStore(tmp_path),
        CostLedger(tmp_path / "stage-costs.jsonl"),
        project,
        registry=_origin_registry(),
        router=Router(execution_mode=ExecutionMode.HEADLESS),
    )
    outcome = eng.record("r1", make_result(work, structured_output=_approved()))
    # The approving verdict carries through untouched — the notice adds an event, not a gate.
    assert outcome["outcome"] not in ("review_rejected_fix_cycle", "review_rejected_exhausted")
    assert eng.store.load_task("r1", "t1").stages[Stage.REVIEW].status.value == "completed"
