"""#73 part 3: ``render_review_plan`` + the size-aware finder set, plan identity, the lane
capability flag, and the per-run ``review_workflow`` opt-in.

The load-bearing property under test is the TRUST BOUNDARY. Adding a lens (``find:design``)
may key off model-influenced context, because more scrutiny is always the safe direction;
DROPPING one is a relaxation and may key only off ENGINE-lane deterministic signals — the
explicit ``change_class`` / ``diff_stat`` parameters — so an implementer that under-reports
its own diff can never talk itself into a thinner review.

Design: docs/reviews/2026-07-09-fable-design-73-review-workflow.md §1/§5 (tests (a), (e)).
"""

from __future__ import annotations

import json
import subprocess

from adapters.execution.base import (
    SUPPORTED,
    CapabilityDescriptor,
    Registry,
    default_registry,
)
from adapters.execution.runners import build_registry
from orchestrator.capacity import CapacityPolicy
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.routing import Router
from orchestrator.schemas.enums import (
    ExecutionLane,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
)
from orchestrator.schemas.work import LanePolicy, TokenUsage, compute_content_hash
from orchestrator.stages import DiffStat, render_prompt, render_review_plan
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

# A diff big enough that no relaxation applies, and one deterministically trivial.
_BIG = DiffStat(files=9, lines=430)
_TRIVIAL = DiffStat(files=1, lines=3)


def _plan(**kw):
    args: dict = dict(task_id="t1", title="Fix the thing", body="do it")
    args.update(kw)
    return render_review_plan(**args)


def _lenses(plan) -> list[str]:
    return [f.lens for f in plan.finders]


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _plan_capable_registry() -> Registry:
    """A small registry whose headless×claude cell executes plans."""
    reg = Registry()
    for mode, provider, plans in (
        (ExecutionMode.HEADLESS, Provider.CLAUDE, True),
        (ExecutionMode.ENGINE, Provider.NONE, False),
    ):
        reg.register_external(CapabilityDescriptor(
            execution_mode=mode, provider=provider, in_process=True,
            schema_enforced=True, supports_plan=plans, status=SUPPORTED,
        ))
    return reg


def _panel_engine(tmp_path, project, **kw) -> Engine:
    """An engine on headless×claude, used to exercise lane-independent panel vetoes."""
    kw.setdefault("registry", _plan_capable_registry())
    kw.setdefault("router", Router(execution_mode=ExecutionMode.HEADLESS))
    return _engine(tmp_path, project, **kw)


def _drive_to_review(eng, *, run="r1", task="t1", util_pct=0.0):
    """Advance a task until REVIEW is the dispatched stage; return that WorkItem."""
    while (w := eng.next_work(run, task, util_pct=util_pct)) is not None:
        if w.stage is Stage.REVIEW:
            return w
        eng.record(run, make_result(w))
    raise AssertionError("task finished without reaching REVIEW")


# --- (a) the finder set: additions, relaxations, and the fixed catalog -------------------

def test_base_panel_is_code_spec_tests() -> None:
    """No relaxation signal and no frontend files: the unrelaxed three-lens panel."""
    assert _lenses(_plan(diff_stat=_BIG)) == ["find:code", "find:spec", "find:tests"]


def test_frontend_files_changed_adds_the_design_finder() -> None:
    """An ADDITION may key off model-influenced context (files_changed) — more scrutiny is
    the safe direction — so a frontend diff resolves to the 4-lens full panel."""
    plan = _plan(context={"files_changed": ["frontend/src/Login.tsx"]}, diff_stat=_BIG)
    assert _lenses(plan) == ["find:code", "find:spec", "find:tests", "find:design"]
    body = plan.finders[-1].prompt
    assert "Visual hierarchy" in body and "Accessibility" in body  # the #62 criteria, reused


def test_backend_change_has_no_design_finder() -> None:
    plan = _plan(context={"files_changed": ["orchestrator/engine.py"]}, diff_stat=_BIG)
    assert "find:design" not in _lenses(plan)


def test_docs_only_drops_the_tests_finder() -> None:
    """The ENGINE-lane docs-only tag is the trusted relaxation the single-reviewer path
    already makes: no behavioral surface, so no dedicated test-meaningfulness dispatch."""
    plan = _plan(change_class="docs-only", diff_stat=_BIG)
    assert _lenses(plan) == ["find:code", "find:spec"]
    # …and every surviving finder is told why, so it doesn't demand unreachable coverage.
    assert all("DOCS-ONLY" in f.prompt for f in plan.finders)


def test_docs_only_frontend_change_still_gets_the_design_finder() -> None:
    plan = _plan(
        change_class="docs-only",
        context={"files_changed": ["docs/theme.css"]},
        diff_stat=_BIG,
    )
    assert _lenses(plan) == ["find:code", "find:spec", "find:design"]


# --- (b) the size-aware ladder ----------------------------------------------------------

def test_trivial_diff_resolves_to_the_code_finder_alone() -> None:
    """A one-line typo fix must not pay for a dedicated spec AND test reviewer, each
    carrying a full context render."""
    assert _lenses(_plan(diff_stat=_TRIVIAL)) == ["find:code"]


def test_diff_just_over_the_trivial_threshold_keeps_the_full_base_set() -> None:
    """Both thresholds must hold: over EITHER one and no size relaxation applies."""
    assert _lenses(_plan(diff_stat=DiffStat(files=1, lines=21))) == [
        "find:code", "find:spec", "find:tests",
    ]
    assert _lenses(_plan(diff_stat=DiffStat(files=3, lines=3))) == [
        "find:code", "find:spec", "find:tests",
    ]


def test_unmeasurable_diff_gets_no_relaxation() -> None:
    """A missing/undeterminable diff_stat fails toward MORE scrutiny, never less."""
    assert _lenses(_plan(diff_stat=None)) == ["find:code", "find:spec", "find:tests"]


def test_trivial_frontend_diff_still_adds_design() -> None:
    """The size ladder relaxes the BASE set; the additive design lens is independent."""
    plan = _plan(diff_stat=_TRIVIAL, context={"files_changed": ["ui/Button.css"]})
    assert _lenses(plan) == ["find:code", "find:design"]


# --- the trust boundary (guard) ---------------------------------------------------------

def test_relaxation_signals_are_ignored_when_only_model_influenced_context_supplies_them() -> None:
    """GUARD. ``context`` is a channel a MODEL writes to. A context that claims docs-only
    and reports a one-file change, with the ENGINE-trusted parameters absent, must produce
    the FULL base panel — an implementer cannot talk itself into a thinner review.

    (The engine passes ``change_class``/``diff_stat`` explicitly, from the ENGINE-lane git
    tag and a real ``git diff --numstat``; this asserts the renderer never sources them from
    the context dict itself.)"""
    lying = {
        "change_class": "docs-only",   # the DETERMINISTIC_ONLY_KEYS-guarded tag, restated
        "files_changed": ["README.md"],  # "look how small and docs-y my change is"
        "diff_stat": {"files": 1, "lines": 1},
        "summary": "trivial one-line docs tweak",
    }
    assert _lenses(_plan(context=lying)) == ["find:code", "find:spec", "find:tests"]
    # And the docs-only directive itself does NOT ride the finder prompts off that claim.
    assert all("DOCS-ONLY" not in f.prompt for f in _plan(context=lying).finders)


# --- prompts: shared framing + one lens each --------------------------------------------

def test_every_finder_shares_the_cache_stable_framing_and_differs_only_by_lens() -> None:
    context = {"files_changed": ["frontend/App.tsx"], "pr_url": "http://x/pr/1"}
    commands = {"test": "uv run pytest"}
    plan = _plan(context=context, project_commands=commands, diff_stat=_BIG)
    shared = "\n\n".join(
        render_prompt(
            Stage.REVIEW, task_id="t1", title="Fix the thing", body="do it",
            context=context, project_commands=commands,
        ).split("\n\n## REVIEW\n")[0:1]
    )
    for finder in plan.finders:
        assert finder.prompt.startswith(shared)  # identical cache-stable prefix
        assert finder.schema_ref == "review_findings"
    # Blind by construction: no finder's prompt names another finder's lens.
    for finder in plan.finders:
        others = [f.lens for f in plan.finders if f.lens != finder.lens]
        assert all(other not in finder.prompt for other in others)


def test_prior_attempt_learnings_ride_every_finder() -> None:
    """A review fix cycle's learnings must reach the panel, exactly as they reach the
    single-reviewer prompt."""
    plan = _plan(learnings="review rejected (cycle 1): the guard is missing", diff_stat=_BIG)
    assert all("## Prior attempts (learn from these)" in f.prompt for f in plan.finders)
    assert all("the guard is missing" in f.prompt for f in plan.finders)


def test_verify_template_is_engine_authored_with_mechanical_slots() -> None:
    plan = _plan(diff_stat=DiffStat(files=2, lines=30, changed_files=("a.py", "tests/test_a.py")))
    assert "{finding}" in plan.verify_template and "{diff_hint}" in plan.verify_template
    assert plan.diff_hint == (
        "Engine-measured changed files (`git diff --numstat`):\n- a.py\n- tests/test_a.py"
    )
    assert "refute" in plan.verify_template.lower()
    assert plan.verify_schema_ref == "review_verdict"
    assert plan.dedupe_rule == "fingerprint-v1"


def test_diff_hint_falls_back_to_reported_files_and_changes_plan_identity() -> None:
    """Even without a measurable git diff the engine authors useful change context, and
    changing that verifier context changes what the dispatch is."""
    first = _plan(context={"files_changed": ["src/a.py"]}, diff_stat=None)
    second = _plan(context={"files_changed": ["src/b.py"]}, diff_stat=None)
    assert first.diff_hint == "Changed files reported by the implementation stage:\n- src/a.py"

    base = dict(stage=Stage.REVIEW, prompt="p", schema_ref="review",
                model="claude-opus-5", attempt=0,
                lane_policy=_lane(ExecutionMode.HEADLESS, Provider.CLAUDE))
    assert compute_content_hash(**base, plan=first) != compute_content_hash(**base, plan=second)
    # `files_changed` also rewrites the finder prompts, so the inequality above proves only
    # that SOMETHING in the plan is hashed. Isolate the field: two plans identical except
    # for `diff_hint` must still hash apart, and a plan re-hashed unchanged must not.
    only_hint = first.model_copy(update={"diff_hint": first.diff_hint + " (rephrased)"})
    assert only_hint.finders == first.finders
    assert compute_content_hash(**base, plan=only_hint) != compute_content_hash(**base, plan=first)
    assert compute_content_hash(**base, plan=first) == compute_content_hash(**base, plan=first)


# --- agent resolution -------------------------------------------------------------------

def test_agents_resolve_through_the_roster_including_the_latent_review_spec_key() -> None:
    """``review:spec`` has been a dead roster key since #62; the spec finder is what finally
    reaches it. An unmapped sub-role resolves to None = the base reviewer persona."""
    roster = {("review", Stage.REVIEW): "code-reviewer", ("spec", Stage.REVIEW): "spec-reviewer"}

    def agent_for(stage: Stage, role: str | None = None) -> str | None:
        return roster.get((role or "", stage))

    plan = _plan(agent_for=agent_for, diff_stat=_BIG, context={"files_changed": ["a.tsx"]})
    agents = {f.lens: f.agent for f in plan.finders}
    assert agents["find:code"] == "code-reviewer"
    assert agents["find:spec"] == "spec-reviewer"
    assert agents["find:tests"] is None  # unmapped -> base reviewer persona
    assert agents["find:design"] is None


def test_no_agent_resolver_leaves_every_finder_on_the_base_persona() -> None:
    assert all(f.agent is None for f in _plan(diff_stat=_BIG).finders)


# --- (a) identity: two finder sets, two content hashes ----------------------------------

def test_two_finder_sets_yield_two_content_hashes() -> None:
    """The plan is CONTENT: a REVIEW dispatch's finder set is part of what the work IS."""
    base = dict(stage=Stage.REVIEW, prompt="p", schema_ref="review",
                model="claude-opus-5", attempt=0,
                lane_policy=_lane(ExecutionMode.HEADLESS, Provider.CLAUDE))
    trivial = compute_content_hash(**base, plan=_plan(diff_stat=_TRIVIAL))
    full = compute_content_hash(**base, plan=_plan(diff_stat=_BIG))
    frontend = compute_content_hash(
        **base, plan=_plan(diff_stat=_BIG, context={"files_changed": ["a.tsx"]})
    )
    assert len({trivial, full, frontend, compute_content_hash(**base)}) == 4
    # Deterministic: the same inputs re-render to the same plan and so the same hash.
    assert full == compute_content_hash(**base, plan=_plan(diff_stat=_BIG))


# --- the lane capability flag (#73 design §5) -------------------------------------------

def test_only_plan_capable_cells_declare_supports_plan() -> None:
    """#288: the flag means "this runner EXECUTES a plan", not "this runner could". A cell
    that over-promises does not degrade gracefully — it degrades SILENTLY, because the engine
    has nothing to veto and so emits no ``review_workflow_skipped``."""
    reg = build_registry()
    claude_headless = reg.describe(
        _lane(ExecutionMode.HEADLESS, Provider.CLAUDE)
    )
    codex_headless = reg.describe(_lane(ExecutionMode.HEADLESS, Provider.CODEX))
    interactive = reg.describe(_lane(ExecutionMode.INTERACTIVE, Provider.CLAUDE))
    engine_lane = reg.describe(_lane(ExecutionMode.ENGINE, Provider.NONE))
    assert claude_headless.supports_plan  # run_review_panel: the reference panel lane
    assert not codex_headless.supports_plan  # no sub-agent primitive
    assert not engine_lane.supports_plan  # no model at all
    assert interactive.supports_plan  # workflow_shim.js runs the finder/verifier panel
    # The 3a default registry declares the same for its interactive cell.
    assert default_registry().describe(
        _lane(ExecutionMode.INTERACTIVE, Provider.CLAUDE)
    ).supports_plan


def _lane(mode: ExecutionMode, provider: Provider) -> LanePolicy:
    return LanePolicy(execution_mode=mode, provider=provider)


# --- (e) the regression: flag OFF is byte-identical to pre-#73 --------------------------

def test_flag_off_dispatches_a_byte_identical_plan_less_review(tmp_path, project) -> None:
    """Design test (e). The plan-less path is the permanent fallback, not scaffolding: with
    ``review_workflow`` off the WorkItem, its prompt, and its content_hash are exactly what
    the pre-#73 engine emitted."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _drive_to_review(eng)
    assert w.plan is None
    task = eng.store.load_task("r1", "t1")
    assert w.prompt == render_prompt(
        Stage.REVIEW, task_id="t1", title=task.title, body=task.body,
        learnings="\n".join(task.learnings), context=task.context,
        project_commands=eng._project_commands(),
        # #302: this engine's default lane is interactive×claude, which cannot enforce
        # REVIEW's tool posture, so its prompt carries the in-band posture directive. Still
        # the plan-less path — the equality being asserted is "the engine renders exactly
        # what render_prompt renders for THIS dispatch", which is what #73 was about.
        tool_posture_unenforced=True,
    )
    # The pre-#73 hash formula (no plan part in the blob).
    assert w.content_hash == compute_content_hash(
        stage=Stage.REVIEW, prompt=w.prompt, schema_ref=w.schema_ref, model=w.model,
        lane_policy=w.lane_policy, attempt=w.attempt, effort=w.effort,
    )


def test_flag_on_attaches_a_plan_and_changes_the_hash(tmp_path, project) -> None:
    # BOTH engines on the plan-capable lane, so the only difference between the two hashes is
    # the plan itself (the lane_policy is part of the hash blob too).
    off = _panel_engine(tmp_path / "off", project)
    off.create_run("r1")
    off.add_task("r1", "t1")
    plain = _drive_to_review(off)

    on = _panel_engine(tmp_path / "on", project)
    on.create_run("r1", review_workflow=True)
    on.add_task("r1", "t1")
    w = _drive_to_review(on)

    assert w.plan is not None
    assert _lenses(w.plan) == ["find:code", "find:spec", "find:tests"]
    assert w.prompt == plain.prompt  # the single-reviewer prompt is unchanged…
    assert w.content_hash != plain.content_hash  # …but the plan makes it different work
    assert w.content_hash == compute_content_hash(
        stage=Stage.REVIEW, prompt=w.prompt, schema_ref=w.schema_ref, model=w.model,
        lane_policy=w.lane_policy, attempt=w.attempt, effort=w.effort, plan=w.plan,
    )


def test_no_plan_rides_a_non_review_stage(tmp_path, project) -> None:
    eng = _panel_engine(tmp_path, project)
    eng.create_run("r1", review_workflow=True)
    eng.add_task("r1", "t1")
    seen = {}
    while (w := eng.next_work("r1", "t1")) is not None:
        seen[w.stage] = w.plan
        eng.record("r1", make_result(w))
    assert seen[Stage.REVIEW] is not None
    assert all(plan is None for stage, plan in seen.items() if stage is not Stage.REVIEW)


# --- the vetoes -------------------------------------------------------------------------

def _skip_reasons(eng, run="r1") -> list[str]:
    return [
        e["reason"] for e in eng.store.read_events(run)
        if e.get("type") == "review_workflow_skipped"
    ]


def test_codex_lane_never_carries_a_plan(tmp_path, project) -> None:
    """The plan is in content_hash, so it may only ride a lane that can execute it."""
    reg = Registry()
    for mode, provider, plans in (
        (ExecutionMode.HEADLESS, Provider.CODEX, False),
        (ExecutionMode.ENGINE, Provider.NONE, False),
    ):
        reg.register_external(CapabilityDescriptor(
            execution_mode=mode, provider=provider, in_process=True,
            schema_enforced=True, supports_plan=plans, status=SUPPORTED,
        ))
    eng = _engine(
        tmp_path, project, registry=reg,
        router=Router(execution_mode=ExecutionMode.HEADLESS,
                      orchestrator_provider=Provider.CODEX),
    )
    eng.create_run("r1", review_workflow=True)
    eng.add_task("r1", "t1")
    w = _drive_to_review(eng)
    assert w.lane_policy.provider is Provider.CODEX
    assert w.plan is None
    assert _skip_reasons(eng) == ["lane_cannot_execute_plan"]


def test_micro_and_lite_presets_never_use_the_panel(tmp_path, project) -> None:
    """They exist to be cheap — a 3-agent panel is exactly what they opted out of."""
    for lane in (ExecutionLane.MICRO, ExecutionLane.LITE):
        eng = _panel_engine(tmp_path / lane.value, project)
        eng.create_run("r1", lane, review_workflow=True)
        eng.add_task("r1", "t1")
        w = _drive_to_review(eng)
        assert w.plan is None
        assert _skip_reasons(eng) == ["cheap_lane_preset"]


def test_a_loaded_api_falls_back_to_the_single_reviewer(tmp_path, project) -> None:
    eng = _panel_engine(tmp_path, project)
    eng.create_run("r1", review_workflow=True)
    eng.add_task("r1", "t1")
    # 75% util is inside the DOWNGRADE band (>= 70, < the 90 wait gate).
    w = _drive_to_review(eng, util_pct=75.0)
    assert w.plan is None
    assert _skip_reasons(eng) == ["capacity_band"]


def test_a_thinning_budget_falls_back_to_the_single_reviewer(tmp_path, project) -> None:
    """A panel is 3–4 model calls where one would do; a run down to its last few percent
    of budget spends that on shipping, not on scrutiny."""
    fat = _panel_engine(tmp_path / "fat", project)
    fat.create_run("r1", review_workflow=True, budget_usd=10.0)
    fat.add_task("r1", "t1")
    assert _drive_to_review(fat).plan is not None  # plenty of budget: the panel runs

    eng = _panel_engine(tmp_path / "thin", project)
    eng.create_run("r1", review_workflow=True, budget_usd=1.0)
    eng.add_task("r1", "t1")
    # SCOPE runs on the deep-reason tier at $5/Mtok input: 190k input tokens ≈ $0.95, which
    # leaves 5% of the budget — the cost router's cheapest band.
    while (w := eng.next_work("r1", "t1")) is not None and w.stage is not Stage.REVIEW:
        tokens = TokenUsage(input=190_000, output=0) if w.stage is Stage.SCOPE else None
        eng.record("r1", make_result(w, tokens=tokens))
    assert 0 < eng.ledger.metered_spend() < 1.0  # thinned, but not hard-stopped
    assert w is not None and w.plan is None
    assert _skip_reasons(eng) == ["budget_thinning"]


# --- #288: a requested panel that never ran must leave a trace ---------------------------
#
# The silent-degradation hole: a lane declaring ``supports_plan`` whose runner ignores
# ``wi.plan`` returns no ``sub_results``, so the engine's fold is skipped, the runner's own
# single-reviewer output is accepted verbatim, and NOTHING recorded that a panel was asked
# for and not delivered — a `review_workflow: true` run produced reviews byte-indistinguishable
# from single-reviewer ones (the #261 defect class).


def _not_executed(eng, run="r1") -> list[dict]:
    return [e for e in eng.store.read_events(run)
            if e.get("type") == "review_plan_not_executed"]


def _review_log(root, task="t1") -> dict:
    """The most recent REVIEW stage-log payload — the durable per-dispatch record."""
    return json.loads(sorted((root / "stages" / task).glob("*-review.json"))[-1].read_text())


def test_a_plan_ignoring_runner_leaves_a_durable_machine_readable_trace(tmp_path, project):
    """Acceptance (a): event + stage-log marker, and NO ``panel_summary`` to be miscounted."""
    eng = _panel_engine(tmp_path, project)
    eng.create_run("r1", review_workflow=True)
    eng.add_task("r1", "t1")
    w = _drive_to_review(eng)
    assert w.plan is not None  # a panel WAS requested
    assert eng.store.load_task("r1", "t1").pending_plan is True  # …and durably remembered

    eng.record("r1", make_result(w))  # the plan-ignoring runner: no sub_results

    events = _not_executed(eng)
    assert len(events) == 1
    ev = events[0]
    assert ev["level"] == "warning"  # warning-grade, not an error: a degraded review is a review
    assert ev["stage"] == "review" and ev["work_item_id"] == w.id
    assert ev["lane"] == "headless:claude"  # names the lane that lied
    assert ev["attempt"] == w.attempt and ev["status"] == "success"

    payload = _review_log(tmp_path)
    assert payload["review_plan_not_executed"] is True
    assert "panel_summary" not in payload  # absence, now PAIRED with a positive marker
    assert "sub_results" not in payload
    # …and the review itself is untouched: the verdict is still the one the runner reported.
    assert payload["structured_output"]["approved"] is True
    assert payload["status"] == "success"
    # The human-readable stage record says so too (no stage-log spelunking required).
    md = sorted((tmp_path / "stages" / "t1").glob("*-review.md"))[-1].read_text()
    assert "Panel requested but NOT executed" in md


def test_a_panel_that_did_run_emits_no_marker(tmp_path, project) -> None:
    """Acceptance (b): no false positives — a plan-bearing dispatch that returns panel
    output folds as always and carries neither the event nor the stage-log marker."""
    eng = _panel_engine(tmp_path, project)
    eng.create_run("r1", review_workflow=True)
    eng.add_task("r1", "t1")
    w = _drive_to_review(eng)
    eng.record("r1", make_result(w, sub_results={
        "findings_by_lens": {"find:code": {"findings": []}}, "verdicts": [],
    }))
    assert _not_executed(eng) == []
    payload = _review_log(tmp_path)
    assert "review_plan_not_executed" not in payload
    assert payload["panel_summary"]["finders"] == 1  # the fold ran
    assert eng.status("r1")["review_panel"]["plan_not_executed"] == 0


def test_a_plan_less_review_never_trips_the_marker(tmp_path, project) -> None:
    """The permanent fallback stays quiet: no plan was dispatched, so a review with no
    sub_results is exactly what was asked for and the payload stays byte-identical."""
    eng = _engine(tmp_path, project)  # flag off, default lane
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _drive_to_review(eng)
    assert w.plan is None
    eng.record("r1", make_result(w))
    assert _not_executed(eng) == []
    assert "review_plan_not_executed" not in _review_log(tmp_path)


def test_a_rate_limited_plan_bearing_review_is_not_flagged(tmp_path, project) -> None:
    """The provider never ran the WorkItem (the stage goes back on the wire, attempt intact),
    so its plan's non-execution is not a fact about the runner — flagging it would cry wolf
    on a run whose retry does execute the panel."""
    eng = _panel_engine(tmp_path, project)
    eng.create_run("r1", review_workflow=True)
    eng.add_task("r1", "t1")
    w = _drive_to_review(eng)
    eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED))
    assert _not_executed(eng) == []
    assert eng.status("r1")["review_panel"]["clean"] is True


def test_the_default_interactive_lane_can_carry_a_plan(tmp_path, project) -> None:
    """The capability declaration reaches assignment: an opted-in interactive REVIEW gets
    the plan that the Workflow shim now executes, with no lane-capability skip."""
    eng = _engine(tmp_path, project)  # default registry + router: interactive×claude
    eng.create_run("r1", review_workflow=True)
    eng.add_task("r1", "t1")
    w = _drive_to_review(eng)
    assert w.plan is not None
    assert _skip_reasons(eng) == []

    audit = eng.status("r1")["review_panel"]
    assert audit["clean"] is True
    assert audit["workflow_skipped"] == 0
    assert audit["plan_not_executed"] == 0


def test_status_tallies_plan_not_executed_per_task(tmp_path, project) -> None:
    """Acceptance: visible from a poll, per task, without opening a stage log."""
    eng = _panel_engine(tmp_path, project)
    eng.create_run("r1", review_workflow=True)
    for task in ("t1", "t2"):
        eng.add_task("r1", task)
        eng.record("r1", make_result(_drive_to_review(eng, task=task)))
    audit = eng.status("r1")["review_panel"]
    assert audit["clean"] is False
    assert audit["plan_not_executed"] == 2
    assert audit["plan_not_executed_by_task"] == {"t1": 1, "t2": 1}
    assert audit["notices"] == 0 and audit["workflow_skipped"] == 0


def test_the_dispatched_plan_marker_never_outlives_its_lease(tmp_path, project) -> None:
    """Acceptance (d): ``pending_plan`` is set with the lease and cleared with it — by
    ``record`` and by ``abandon`` — so it can never mis-attribute a LATER dispatch."""
    eng = _panel_engine(tmp_path, project)
    eng.create_run("r1", review_workflow=True)
    eng.add_task("r1", "t1")
    # Every pre-REVIEW dispatch is plan-less, and the marker stays False throughout.
    while (w := eng.next_work("r1", "t1")) is not None and w.stage is not Stage.REVIEW:
        assert eng.store.load_task("r1", "t1").pending_plan is False
        eng.record("r1", make_result(w))
    assert w is not None and w.plan is not None
    assert eng.store.load_task("r1", "t1").pending_plan is True
    eng.record("r1", make_result(w))
    assert eng.store.load_task("r1", "t1").pending_plan is False  # cleared with the lease

    # …and the abandon path clears it too (a stuck plan-bearing dispatch).
    eng2 = _panel_engine(tmp_path / "abandoned", project)
    eng2.create_run("r1", review_workflow=True)
    eng2.add_task("r1", "t1")
    assert _drive_to_review(eng2).plan is not None
    assert eng2.store.load_task("r1", "t1").pending_plan is True
    eng2.abandon("r1", "t1", reason="stuck", force=True)
    assert eng2.store.load_task("r1", "t1").pending_plan is False


def test_pending_plan_defaults_false_on_a_pre_change_task_doc(tmp_path, project) -> None:
    """Additive field: a task doc written before #288 loads with the marker off, which reads
    as 'no plan was dispatched' — no false marker on an in-flight upgrade."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    path = tmp_path / "status-r1-t1.json"
    stripped = path.read_text().replace('"pending_plan": false,', "")
    assert stripped != path.read_text()  # the field really was there to strip
    path.write_text(stripped)
    assert eng.store.load_task("r1", "t1").pending_plan is False


# --- persistence across the CLI process boundary (#206) ---------------------------------

def test_review_workflow_survives_a_create_then_separate_subcommand(tmp_path, project) -> None:
    """The #206 invariant: every CLI subcommand rebuilds the Engine from constructor
    DEFAULTS, so the opt-in must be re-read off the Run doc at the dispatch boundary."""
    creator = _panel_engine(tmp_path, project)
    creator.create_run("r1", review_workflow=True)
    creator.add_task("r1", "t1")
    assert creator.store.load_run("r1").review_workflow is True

    # A FRESH Engine, exactly as `cli._engine` builds one — nothing carried in memory.
    fresh = _panel_engine(tmp_path, project)
    w = _drive_to_review(fresh)
    assert w.plan is not None
    assert _lenses(w.plan) == ["find:code", "find:spec", "find:tests"]


def test_review_workflow_defaults_off_on_a_pre_change_run_doc(tmp_path, project) -> None:
    """Additive field: a run doc written before #73 loads with the flag off."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    path = tmp_path / "status-r1.json"
    path.write_text(path.read_text().replace('"review_workflow": false,', ""))
    assert eng.store.load_run("r1").review_workflow is False


# --- the deterministic diff-stat read ---------------------------------------------------

def test_diff_stat_is_read_from_a_real_git_diff(tmp_path, project) -> None:
    """The size signal is the ENGINE's OWN measurement, not a model's self-report."""
    repo = tmp_path / "wt"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    (repo / "a.py").write_text("".join(f"x = {i}\n" for i in range(30)))
    (repo / "b.py").write_text("y = 2\n")
    git("add", "-A")
    git("commit", "-qm", "work")

    eng = _engine(tmp_path / "store", project)
    eng.create_run("r1")
    task = eng.add_task("r1", "t1")
    task.context.update({"base_sha": base, "worktree": str(repo)})
    stat = eng._deterministic_diff_stat(task)
    assert stat is not None and stat.files == 2 and stat.lines >= 30
    assert stat.changed_files == ("a.py", "b.py")


def test_diff_stat_is_none_when_it_cannot_be_measured(tmp_path, project) -> None:
    """Any failure returns None, and None means NO relaxation (full panel)."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    task = eng.add_task("r1", "t1")
    assert eng._deterministic_diff_stat(task) is None  # no base_sha / no worktree
    task.context.update({"base_sha": "deadbeef", "worktree": str(tmp_path / "nope")})
    assert eng._deterministic_diff_stat(task) is None  # worktree gone
    task.context["worktree"] = str(tmp_path)  # exists, but not a git repo with that sha
    assert eng._deterministic_diff_stat(task) is None


def test_capacity_policy_band_edge_is_the_one_the_gate_uses(tmp_path, project) -> None:
    """A project that widens the NORMAL band widens the panel's eligibility with it —
    the gate reads the injected CapacityPolicy, not a hardcoded 70."""
    eng = _panel_engine(tmp_path, project, capacity=CapacityPolicy(downgrade_threshold=85.0))
    eng.create_run("r1", review_workflow=True)
    eng.add_task("r1", "t1")
    w = _drive_to_review(eng, util_pct=75.0)
    assert w.plan is not None
    assert _skip_reasons(eng) == []
