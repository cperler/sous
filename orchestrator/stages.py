"""Per-stage specs + prompt rendering (target.md §6.1).

The engine owns the *generic* stage scaffolding (what each collapsed stage is for,
its model role, its output schema key, its agent sub-role). Project-specific values
(test commands, agent names, taxonomy) come from the project-config adapter — so the
prompts stay repo-agnostic and the same engine drives any project.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .model_table import Role
from .schemas.enums import STAGE_ORDER, Effort, Stage
from .schemas.work import FinderSpec, ReviewPlan


@dataclass(frozen=True)
class StageSpec:
    stage: Stage
    model_role: str  # resolved to a model id by the model_table
    schema_ref: str  # output-schema key the runner enforces
    agent_role: str | None  # sub-role for ProjectConfig.agent_for()
    template: str  # the stage instruction (goal + return spec); render_prompt frames it
    # Wall-clock ceiling (seconds) the engine threads into the WorkItem so a hung
    # CLI fails as a classifiable TIMEOUT instead of hanging the scheduler forever.
    # Sized by the stage's expected work: cheap shell < reasoning < implement/test.
    timeout_s: int = 900
    # Git-affecting stage (design pass §3): success ends in a tagged commit and a
    # retry/crash-resume resets the worktree to the last-good tag. Vocabulary
    # metadata — the transport wrapper does the git I/O, the engine only names tags.
    checkpoint: bool = False
    # Deterministic stage: produced by an in-process shell/engine runner on the
    # non-model ENGINE lane — NEVER a model call (heysoo #227: don't ask an LLM to run
    # `git worktree add`). Routes to (ExecutionMode.ENGINE, Provider.NONE); $0.
    deterministic: bool = False
    # Default reasoning effort for a model-lane dispatch of this stage (#96): hard
    # reasoning stages (scope/implement) run high, judgment stages (test/review) medium,
    # mechanical prose (deliver) low. None = provider default (and always None on a
    # deterministic stage — the ENGINE lane has no model to throttle). A per-task
    # effort pin overrides this, mirroring how model_pin overrides model_role.
    effort: Effort | None = None


# The 6 collapsed stages. Templates are deliberately terse, goal-plus-constraints
# (newer models do better with that than enumerated micro-steps — design-doc §2).
STAGE_SPECS: dict[Stage, StageSpec] = {
    Stage.INTAKE: StageSpec(
        stage=Stage.INTAKE,
        model_role=Role.CHEAP_SHELL,
        schema_ref="intake",
        agent_role=None,
        timeout_s=1800,  # worktree prep + a REAL baseline test run (bounded at 900s itself)
        checkpoint=True,  # the baseline anchor: implement's first retry resets here
        deterministic=True,  # run by the engine's shell runner, not a model (heysoo #227)
        template=(
            "Prepare an isolated worktree/branch for this task and capture a test "
            "baseline using the project's test commands. Do not implement anything.\n"
            "Return: branch, worktree, baseline_captured."
        ),
    ),
    Stage.SCOPE: StageSpec(
        stage=Stage.SCOPE,
        model_role=Role.DEEP_REASON,
        schema_ref="scope",
        agent_role="scope",
        timeout_s=600,  # deep reasoning, no file edits
        effort=Effort.HIGH,  # feasibility + planning is a hard-reasoning stage (#96)
        template=(
            "Understand the change, decide feasibility, and produce a minimal task "
            "plan. If genuinely blocked, say so.\n"
            "Return: feasible, blocked_reason, plan (list of subtasks)."
        ),
    ),
    Stage.IMPLEMENT: StageSpec(
        stage=Stage.IMPLEMENT,
        model_role=Role.DEEP_REASON,
        schema_ref="implement",
        agent_role="implement",
        timeout_s=1800,  # the heavy stage: multi-file edits + commits
        checkpoint=True,
        effort=Effort.HIGH,  # the heavy reasoning stage (#96)
        template=(
            "Implement the change and commit it. Follow the scope plan in the context "
            "above if present; if none (lite/micro), implement the task spec directly "
            "as a single change.\n"
            "Boy-scout, bounded: fix TRIVIAL issues WITHIN the blast radius of your own "
            "change in place — a docstring your edit made stale, a guard or type on a "
            "line you are already touching — and do NOT file them as follow-ups. But stay "
            "bounded: if a fix would grow the diff into a new subsystem or add real risk, "
            "it is NOT a boy-scout fix — leave it for the reviewer to record as a "
            "legitimate follow-up.\n"
            "Return: files_changed, summary, committed."
        ),
    ),
    Stage.TEST: StageSpec(
        stage=Stage.TEST,
        model_role=Role.REVIEW,
        schema_ref="test",
        agent_role="test",
        timeout_s=1200,  # test/fix iterate-until-green loop
        checkpoint=True,
        effort=Effort.MEDIUM,  # judgment (meaningfulness) but mostly mechanical loops (#96)
        template=(
            "Run the project's tests for the changed files, fix regressions you "
            "introduced, and re-run until green or no progress. Tests listed under "
            "baseline_failures in the context above were ALREADY failing at base — "
            "inherited, not yours: do not fix them and do not count them against this "
            "change. Then VERIFY the tests are meaningful: they must exercise THIS "
            "change and would fail if it regressed — not vacuous, tautological, or "
            "always-green.\n"
            "Return: passed, failures (list of failing test ids), tests_meaningful "
            "(bool — only true if the tests genuinely cover the change), "
            "validation_notes (what the tests assert / any gaps)."
        ),
    ),
    Stage.DELIVER: StageSpec(
        stage=Stage.DELIVER,
        model_role=Role.REVIEW,
        schema_ref="deliver",
        agent_role="docstring",  # generic docstring agent (fix D13, no phpdoc-writer)
        timeout_s=600,  # docstrings + open a PR
        checkpoint=True,
        effort=Effort.LOW,  # mechanical prose + `gh pr create` — the cheap stage (#96)
        template=(
            "Add/refresh docstrings for changed source, then open a pull request for "
            "the task branch. If the task is a GitHub issue (#N), include 'Closes #N' "
            "in the PR description so the merge closes the issue. If the context above "
            "already shows a pr_url for this task (a review fix cycle), push the "
            "branch and reuse that PR — never open a duplicate.\n"
            "Return: pr_number, pr_url."
        ),
    ),
    Stage.REVIEW: StageSpec(
        stage=Stage.REVIEW,
        model_role=Role.REVIEW,
        schema_ref="review",
        agent_role="review",
        timeout_s=600,  # read the PR + judge
        effort=Effort.MEDIUM,  # careful judgment over a bounded diff (#96)
        template=(
            "Review the PR (see pr_url in the context above) against the task goal and "
            "code quality. Assess the goal criterion-by-criterion and check for "
            "regressions; approve only if it achieves the goal without regressions. "
            "INDEPENDENTLY verify the change's tests: read them and judge whether they "
            "meaningfully exercise this change (would they fail if it regressed, or are "
            "they vacuous/tautological/always-green?) — report tests_meaningful (bool); "
            "you are a different agent from the one that wrote them, which is the point. "
            "Separately, record any NON-BLOCKING findings (nits, edge cases, polish, "
            "follow-on ideas) that must not hold up this PR, and CLASSIFY each with a "
            "`disposition` against the filing bar — would a maintainer put this on the "
            "backlog if they'd noticed it independently, without staring at this exact "
            "diff? `file` only if it clears that bar: out-of-scope (different subsystem / "
            "needs its own design) OR non-trivial (real work/risk). NEVER `file` these — "
            "they are `drop` (or `fix_now` if trivial-in-blast-radius): cosmetic "
            "consistency ('move X to match Y', rename for symmetry), speculative "
            "tunability (expose knobs nobody asked for), premature instrumentation "
            "(dashboards/metrics for a feature not yet exercised), redundant convenience "
            "(a wrapper for already-exposed capability). `fix_now` for a "
            "trivial nit inside the change's blast radius the implementer should have "
            "absorbed in place (a docstring its edit made stale, a guard/type on a touched "
            "line) — do NOT inflate these into tickets. `drop` for a real-but-untracked "
            "observation. The engine files only `file` findings (up to a small per-task "
            "cap) and surfaces the rest in the completion note, so nothing is silently "
            "dropped without ballooning the backlog. Do NOT restate one idea as both a "
            "non_blocking finding and the `improvement` below — pick one.\n"
            "Finally — the self-improvement loop — step back from THIS PR and propose: "
            "(a) improvement — the single highest-value forward-looking enhancement this "
            "task suggests for the PROJECT/roadmap. Emit one ONLY if a maintainer would "
            "independently prioritize it (a concrete trigger / demonstrated need); do NOT "
            "emit speculative tunability, premature instrumentation, or convenience "
            "wrappers for already-exposed capability. Classify it with a `disposition`: "
            "`file` (default) files it as an enhancement issue; `fixup` means apply it as "
            "a trivial in-place change in THIS PR (do not file); `fix_now`/`drop` are "
            "surfaced in the completion note, not filed. Omit the improvement entirely if "
            "none clears the bar. And (b) retrospective — one lesson this task teaches "
            "about the ORCHESTRATION PROCESS itself (prompts, stages, tooling, lanes). "
            "Omit either if nothing genuine stands out — do not pad.\n"
            "Return: approved, issues (blocking; empty when approved — each an object "
            "{severity: critical|important|suggestion, file, line, description, "
            "suggested_fix}; a rejection re-runs implement→…→review with your issues as "
            "learnings, so make them concrete and actionable; suggestion-only rejections "
            "auto-approve), non_blocking (list of {title, detail, disposition: "
            "file|fix_now|drop}; empty if none), improvement ({title, detail, disposition: "
            "file|fixup|fix_now|drop} or omitted), retrospective ({title, detail} or "
            "omitted)."
        ),
    ),
}


# Frontend-file signals for the design-review lens (#62). Any changed path with one of
# these suffixes, or living under a ``frontend/`` segment, marks the change as user-facing.
# Framework-neutral so the lens fires for React/Vue/Svelte/plain-CSS projects alike.
_FRONTEND_SUFFIXES: tuple[str, ...] = (
    ".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".sass", ".less",
)


def _has_frontend_change(files_changed: object) -> bool:
    """True if any changed file looks user-facing (a design surface). Deterministic and
    engine-template-side: a pure function of ``files_changed``, so the same context always
    yields the same lens. Unlike the #41 docs-only tag this need not be ENGINE-lane-trusted
    — the lens only ADDS review scrutiny, so a model over- or under-reporting files can't
    exploit it to skip work (the safe direction)."""
    if not isinstance(files_changed, list):
        return False
    for f in files_changed:
        p = str(f).lower()
        if p.endswith(_FRONTEND_SUFFIXES) or "frontend/" in p:
            return True
    return False


# The design-craft criteria themselves (#62), factored out so the single-reviewer lens below
# and the #73 ``find:design`` finder body share ONE list — they must not drift. Project-
# agnostic: the heysoo-specific design-system tokens (visual language, component library,
# theme rules) stay in the adapter's design agent; this block is the reusable craft lens only.
_DESIGN_CRITERIA = (
    "- Visual hierarchy: size/weight/spacing guide attention; the primary element reads first.\n"
    "- Spacing & alignment: a consistent scale (e.g. an 8pt grid), no arbitrary one-off values.\n"
    "- Consistency & reuse: reuse existing components/patterns/tokens over reinventing them.\n"
    "- Accessibility: sufficient contrast, keyboard operability, visible focus, labels/roles, "
    "adequate tap targets; never rely on color alone.\n"
    "- Responsive behavior: works across viewport sizes and larger text; no fixed heights on "
    "text containers; graceful with more/less content.\n"
)

# Project-agnostic design-review criteria injected into the REVIEW prompt when the change
# touches frontend files (#62).
_DESIGN_REVIEW_LENS = (
    "\n\n## Frontend change: apply the design-review lens\n"
    "This change touches user-facing files (per files_changed above). Beyond correctness, "
    "review the design craft:\n"
    + _DESIGN_CRITERIA
    + "Treat these as review criteria — blocking only when a change materially harms usability "
    "or accessibility; otherwise record them as non-blocking polish."
)


# The #41 docs-only directive, verbatim. Hoisted to a constant so the single-reviewer
# prompt (``render_prompt``) and a workflow finder prompt (``render_review_plan``) say the
# SAME thing about a deterministically-classified docs-only change — one source of truth,
# and the hoist keeps ``render_prompt``'s output byte-identical.
_DOCS_ONLY_DIRECTIVE = (
    "\n\n## Change classification: DOCS-ONLY\n"
    "This change was deterministically classified as documentation-only (every "
    "changed file is docs). It has no behavioral surface, so DO NOT apply "
    "test-coverage criteria: treat tests_meaningful as satisfied and do not reject "
    "or hold this change for lacking new/updated tests. Judge it on documentation "
    "correctness and clarity instead."
)


# The #13/#168/#261 tests_meaningful directive for the single-reviewer REVIEW prompt.
# Hoisted next to _DOCS_ONLY_DIRECTIVE for the same reason: one wording, one place.
#
# #261: the omission carve-out is scoped EXPLICITLY to a genuinely absent test surface. The
# old wording ("Only set it when there ARE tests to judge … OMIT the field entirely") read as
# broad permission to omit, and on a lite-lane task with 7 new tests the reviewer took it —
# while the deterministic TEST runner was deferring to REVIEW. Each side deferred to the
# other and nobody judged. So: state that an omission is recorded as "not judged" and
# evented, never as a pass. The `false`-is-a-rejection warning stays (it is what #144/#168
# exist for) — it just no longer doubles as an excuse to skip the judgment.
_TESTS_MEANINGFUL_DIRECTIVE = (
    "\n\n## Reporting tests_meaningful\n"
    "JUDGE `tests_meaningful` whenever this change has tests you can read — including tests "
    "it adds, changes, or should have added. That judgment is your job here: you are a "
    "different agent from the one that wrote them, which is the whole point of the check. "
    "OMIT the field ONLY when there is genuinely NO test surface: a docs/config-only change, "
    "or nothing behavioral that any test could exercise.\n"
    "An omission is recorded as **not judged** and is evented as a skipped verification — it "
    "is NOT a pass, and it does not save you the work. But do not answer `false` merely "
    "because a change has no tests to judge: a literal `false` reads as a rejection for "
    "having vacuous tests and drives a fix cycle. `false` means \"there ARE tests and they "
    "would NOT fail if this change regressed\"."
)


def _render_value(v: object) -> str:
    """One folded context value → a compact one-line-ish string for the prompt."""
    if isinstance(v, list):
        return "; ".join(str(x) for x in v) if v else "(none)"
    if v is None:
        # A folded null is an explicit ABSTENTION, not the string "None" (#261: the
        # deterministic TEST runner reports `tests_meaningful: null` because it cannot judge
        # meaningfulness). Render it as such so a downstream prompt reads it correctly.
        return "(not reported)"
    return str(v)


def _render_context(context: dict) -> str:
    """Render the folded task context in FIXED canonical (pipeline) order so the block is
    a pure function of the values — byte-identical on replay, cache-stable across a run."""
    # Imported here (not module-scope) to keep the stage-metadata module free of an
    # import-time dependency on the state machine; the map is the single fold source.
    from .state_machine import CONTEXT_KEYS

    lines = [
        f"- {key}: {_render_value(context[key])}"
        for stage in STAGE_ORDER
        for key in CONTEXT_KEYS[stage]
        if key in context
    ]
    return "\n".join(lines)


def _shared_sections(
    *,
    task_id: str,
    title: str,
    body: str,
    context: dict | None = None,
    project_commands: dict[str, str] | None = None,
) -> list[str]:
    """The cache-stable framing sections 1–3(+KB recall) every dispatch for a task shares:

      1. project commands  — stable project-wide (identical for every task & stage)
      2. task spec (title/body) — stable per-task (identical across the task's 6 stages)
      3. folded task context — per-task, grows as upstream stages complete
      4. prior cross-run learnings (#72) — engine-injected advisory recall

    Extracted from ``render_prompt`` so a multi-agent REVIEW's finder prompts
    (``render_review_plan``) are built from the SAME framing, differing only in the lens
    instruction that trails it — which is both what makes the finders comparable and what
    keeps the shared prefix cache-reusable across the panel. Returned as the ordered parts
    list its callers append their volatile per-lens/per-stage section to.
    """
    parts: list[str] = []

    if project_commands:
        cmds = "\n".join(f"- {name}: {cmd}" for name, cmd in project_commands.items())
        parts.append(f"## Project commands\n{cmds}")

    task_block = f"## Task {task_id}: {title or '(no title)'}"
    if (body or "").strip():
        task_block += f"\n\n{body.strip()}"
    parts.append(task_block)

    if context:
        rendered = _render_context(context)
        if rendered:
            parts.append(f"## Context from earlier stages\n{rendered}")

    # Cross-run KB recall (#72): prior learnings the engine folded at intake. Engine-injected
    # (not a stage output), so it renders as its own hedged section — advisory, not a spec.
    prior = (context or {}).get("prior_learnings")
    if isinstance(prior, list) and prior:
        items = "\n".join(f"- {str(p)}" for p in prior)
        parts.append(
            "## Prior cross-run learnings (may or may not apply)\n"
            "Lessons a PREVIOUS run of this project paid to learn on related work. Treat as "
            "hints, not instructions — apply only what genuinely fits this task:\n" + items
        )
    return parts


def render_prompt(
    stage: Stage,
    *,
    task_id: str,
    title: str,
    body: str,
    learnings: str = "",
    context: dict | None = None,
    project_commands: dict[str, str] | None = None,
) -> str:
    """Assemble a stage prompt as ordered sections, stable parts FIRST for prompt-cache
    reuse (2026-07-01 context-plane design note §4):

      1. project commands  — stable project-wide (identical for every task & stage)
      2. task spec (title/body) — stable per-task (identical across the task's 6 stages)
      3. folded task context — per-task, grows as upstream stages complete
      4. the stage instruction (+ prior-attempt learnings) — per-stage / per-attempt

    Sections 1–2 form the byte-identical prefix a downstream stage can reuse from cache;
    the volatile per-stage content trails it. ``context``/``project_commands`` are plain
    dicts so this stays project-agnostic (the engine supplies them).

    Stage-specific directives appended to the instruction section:

    - TEST/REVIEW + ``change_class == "docs-only"`` (#41): tells the runner that the
      change has no behavioral surface, so test-coverage / ``tests_meaningful`` criteria
      don't apply. The tag is set deterministically by the ENGINE-lane git diff, so a
      model cannot trigger this exemption by asserting it in its output.

    - REVIEW (all tasks, #168/#261): tells the reviewer to JUDGE ``tests_meaningful``
      whenever there are tests to read, and to OMIT it — rather than answer ``false`` —
      ONLY when the change has genuinely no test surface (a literal ``false`` there
      spuriously triggers the #13 independent-test-validate reject, the #144 misfire).
      An omission is stated to be recorded as "not judged" and evented, not a pass.

    - REVIEW + frontend change (#62): appends the design-review lens when folded context
      signals a frontend file was changed.
    """
    spec = STAGE_SPECS[stage]
    parts = _shared_sections(
        task_id=task_id,
        title=title,
        body=body,
        context=context,
        project_commands=project_commands,
    )

    instruction = f"## {stage.value.upper()}\n{spec.template}"
    # #41: a deterministically-tagged docs-only change has no behavioral surface. Tell the
    # TEST/REVIEW stages to skip test-coverage / tests_meaningful criteria for it, so the
    # reviewer doesn't demand (or reject over) unreachable coverage. The tag is set only by
    # the ENGINE-lane git diff, so this directive can't be triggered by a model's own claim.
    if stage in (Stage.TEST, Stage.REVIEW) and (context or {}).get("change_class") == "docs-only":
        instruction += _DOCS_ONLY_DIRECTIVE
    # #168: belt-and-suspenders with the engine's fail-open-on-omit gate — tell the reviewer
    # to OMIT `tests_meaningful` (rather than answer `false`) when there is no test surface to
    # judge (a docs/config change, or a task whose pipeline runs no meaningful tests). A literal
    # `false` on a no-test-surface change is what spuriously kicked #144 into a fix cycle.
    if stage is Stage.REVIEW:
        instruction += _TESTS_MEANINGFUL_DIRECTIVE
    # #62: a frontend change (deterministic signal on files_changed folded from IMPLEMENT)
    # gets the design-review criteria block appended to the REVIEW prompt. Project-agnostic
    # wording; the heysoo-specific design tokens live in the adapter's design agent.
    if stage is Stage.REVIEW and _has_frontend_change((context or {}).get("files_changed")):
        instruction += _DESIGN_REVIEW_LENS
    if learnings:
        instruction += f"\n\n## Prior attempts (learn from these)\n{learnings}"
    parts.append(instruction)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# #73 multi-agent REVIEW: the finder-lens catalog + deterministic plan resolution
# ---------------------------------------------------------------------------
#
# A plan-bearing REVIEW dispatch fans out BELOW the seam (design §1/§2): the engine
# renders one prompt per lens here, the runner executes them blind to each other, and
# the engine folds the results back into canonical review.json. Everything in this
# section is pure and deterministic — same inputs, same plan, byte-for-byte.

# Schema refs the panel's sub-calls validate against (design §1 "Schemas").
_FINDINGS_SCHEMA_REF = "review_findings"
_VERDICT_SCHEMA_REF = "review_verdict"
# Names the shared normalization both lanes dedupe findings with, so an interactive and a
# headless panel collapse the same duplicates (engine re-dedupes authoritatively at fold).
_DEDUPE_RULE = "fingerprint-v1"

# Every finder returns the same object shape (review_findings.json), stated once.
_FINDER_RETURN = (
    "Return: findings (list of {severity: critical|important|suggestion, file, line, "
    "description, suggested_fix} — empty when you find nothing, which is a valid and "
    "useful answer), improvement ({title, detail} or omitted), retrospective "
    "({title, detail} or omitted)."
)
# The blindness contract, stated to every finder identically.
_FINDER_PREAMBLE = (
    "You are ONE independent finder on a review panel for the change described above. "
    "You cannot see the other finders' output and must not speculate about it — do not "
    "hedge, defer, or assume another lens covers something. Report ONLY findings inside "
    "YOUR lens, and only ones you can evidence from the working tree: every finding needs "
    "a file (and a line where you can give one) and a description concrete enough for a "
    "different agent to verify or refute from the code alone. Do NOT emit a verdict — "
    "approval is decided by the engine from what survives verification, not by you.\n"
)


@dataclass(frozen=True)
class _Lens:
    """One finder lens: its name, the ProjectConfig sub-role its persona resolves through,
    and the instruction body appended after the shared context sections."""

    name: str
    agent_role: str
    body: str


_LENS_CODE = _Lens(
    name="find:code",
    agent_role="review",  # the base code-reviewer persona
    body=(
        "## FIND: CODE — correctness and regressions\n"
        + _FINDER_PREAMBLE
        + "Your lens is CORRECTNESS. Read the change and hunt for: logic errors, "
        "off-by-one/boundary mistakes, unhandled inputs and error paths, regressions to "
        "existing behavior or public interfaces, transaction/partial-failure hazards, "
        "concurrency and ordering bugs, resource leaks, and violations of invariants the "
        "project states about itself. Judge the CODE, not the process.\n"
        "NOT your lens (other finders own these; stay off them): whether the change matches "
        "what was asked, whether the tests are meaningful, and visual/design craft.\n"
        + _FINDER_RETURN
    ),
)

_LENS_SPEC = _Lens(
    name="find:spec",
    # Reaches the latent `review:spec` roster key (#62 shipped it; nothing dispatched it
    # until now). A project without that key resolves None -> the base reviewer persona.
    agent_role="spec",
    body=(
        "## FIND: SPEC — built-vs-asked conformance\n"
        + _FINDER_PREAMBLE
        + "Your lens is CONFORMANCE: does what was BUILT match what was ASKED? Work "
        "criterion-by-criterion through the task spec above (and the scope plan in the "
        "context, if any) and report: acceptance criteria not met, requirements silently "
        "dropped or reinterpreted, shipped behavior nobody asked for (scope the task did "
        "not authorize), and stated constraints violated. Quote the criterion you are "
        "judging against in each finding.\n"
        "NOT your lens: implementation-quality bugs that are on-spec, test meaningfulness, "
        "and visual/design craft.\n"
        + _FINDER_RETURN
    ),
)

_LENS_TESTS = _Lens(
    name="find:tests",
    # Deliberately `tests` (not the `test` implementer role): an INDEPENDENT judge of test
    # meaningfulness, so a project must opt a persona in explicitly rather than inherit the
    # agent that writes the code. Unset -> None -> the base reviewer persona.
    agent_role="tests",
    body=(
        "## FIND: TESTS — do the tests actually hold this change down?\n"
        + _FINDER_PREAMBLE
        + "Your lens is TEST MEANINGFULNESS — the independent test-validation dispatch: you "
        "are a different agent from the one that wrote these tests, which is the point. Read "
        "the change's tests and judge whether they would FAIL if this change regressed. "
        "Report as findings: assertions that are vacuous, tautological, or always-green; "
        "tests that exercise mocks instead of the change; coverage gaps on the change's real "
        "behavior and error paths; and tests that pin implementation detail rather than "
        "behavior.\n"
        "NOT your lens: correctness of the production code itself, spec conformance, and "
        "visual/design craft.\n"
        "Also return tests_meaningful (bool) — your verdict on whether the tests genuinely "
        "cover this change. JUDGE it whenever there are tests to read (including ones this "
        "change adds or should have added); OMIT it ONLY when the change has genuinely no "
        "test surface (docs/config-only, nothing behavioral). An omission is recorded as "
        "'not judged' and evented as a skipped verification — it is not a pass. Do not "
        "answer false merely because tests are absent: false means 'there ARE tests and they "
        "would NOT fail if this change regressed', and it reads as a rejection.\n"
        + _FINDER_RETURN
    ),
)

_LENS_DESIGN = _Lens(
    name="find:design",
    agent_role="design",
    body=(
        "## FIND: DESIGN — frontend craft\n"
        + _FINDER_PREAMBLE
        + "Your lens is DESIGN CRAFT. This change touches user-facing files (per "
        "files_changed above). Review the craft:\n"
        # _DESIGN_REVIEW_LENS reframed as a finder: the SAME project-agnostic criteria the
        # single-reviewer path appends (#62, shared via _DESIGN_CRITERIA), minus its "treat
        # these as review criteria" verdict framing — a finder reports, the engine decides.
        + _DESIGN_CRITERIA
        + "Severity guidance: `critical`/`important` only when a change materially harms "
        "usability or accessibility; otherwise `suggestion`.\n"
        "NOT your lens: backend correctness, spec conformance, and test meaningfulness.\n"
        + _FINDER_RETURN
    ),
)

# The UNRELAXED base panel, in fixed order. `find:design` is appended separately because it
# is an ADDITION keyed off model-influenced context (safe direction), not part of the base.
_BASE_LENSES: tuple[_Lens, ...] = (_LENS_CODE, _LENS_SPEC, _LENS_TESTS)

# Adversarial verification (design §2). Engine-AUTHORED with mechanical slots the runner
# fills per finding — the same `_corrective_prompt` precedent: runners substitute, never
# author. `{finding}` is the deduped finding (fingerprint + severity + file/line +
# description); `{diff_hint}` points the verifier at where to look.
_VERIFY_TEMPLATE = (
    "## VERIFY: try to refute this finding\n"
    "You are the ADVERSARY on a review panel. Another agent reported the finding below "
    "against this change. Your job is to KILL it: read the working tree and try to prove it "
    "wrong — the code path is unreachable, the case is already handled elsewhere, the cited "
    "file/line does not say what the finding claims, or the behavior is intended and "
    "specified. Confirm it ONLY if you cannot refute it with evidence you actually read; "
    "do not confirm on plausibility, and do not soften or re-scope the finding — verdict "
    "on it exactly as written.\n\n"
    "### Finding\n{finding}\n\n"
    "### Where to look\n{diff_hint}\n\n"
    "Return: fingerprint (echo the finding's fingerprint EXACTLY, unmodified), verdict "
    "(confirmed|refuted), reasoning (the evidence you read, cited by file and line)."
)


@dataclass(frozen=True)
class DiffStat:
    """Deterministic size of the change under review — ``files`` touched and ``lines``
    added+deleted, read by the ENGINE lane from ``git diff --numstat`` (never from a model's
    self-report). The ONLY size signal the finder-set relaxation ladder is allowed to key
    off; see ``render_review_plan`` for why."""

    files: int
    lines: int


# Relaxation thresholds (both must hold to call a diff trivial). Sized so a one-line typo
# fix or a small single-file tweak doesn't buy a 3-agent panel, while anything that could
# plausibly hide a spec deviation or a vacuous test still gets the full base set. Named
# constants, not magic numbers, so re-tuning them with evidence is a one-line change.
_TRIVIAL_DIFF_MAX_FILES = 2
_TRIVIAL_DIFF_MAX_LINES = 20


def _is_trivial_diff(diff_stat: DiffStat | None) -> bool:
    """True only for a diff DETERMINISTICALLY measured as tiny. A missing/undeterminable
    ``diff_stat`` is NOT trivial — an unmeasurable change gets the full panel, so the
    failure direction is always toward MORE scrutiny."""
    if diff_stat is None:
        return False
    return (
        diff_stat.files <= _TRIVIAL_DIFF_MAX_FILES and diff_stat.lines <= _TRIVIAL_DIFF_MAX_LINES
    )


def render_review_plan(
    *,
    task_id: str,
    title: str,
    body: str,
    learnings: str = "",
    context: dict | None = None,
    project_commands: dict[str, str] | None = None,
    agent_for: Callable[[Stage, str | None], str | None] | None = None,
    change_class: str | None = None,
    diff_stat: DiffStat | None = None,
) -> ReviewPlan:
    """Resolve the multi-agent REVIEW plan deterministically at ``next_work`` (design §1).

    Each finder prompt is ``_shared_sections`` (project commands → task spec → folded
    context → prior learnings) plus ONLY its own lens instruction, so the finders are blind
    to each other by construction and share a cache-stable prefix. ``learnings`` (prior
    attempts / a review fix cycle) trails every finder's lens, exactly as it trails the
    stage instruction in ``render_prompt``.

    **The trust boundary, which is the load-bearing constraint here.** Per
    ``DETERMINISTIC_ONLY_KEYS``:

    - *Adding* a lens may key off model-influenced context — ``find:design`` fires on
      ``_has_frontend_change(context["files_changed"])`` — because more scrutiny is always
      the safe direction: an implementer over-reporting its files only buys itself a
      stricter panel.
    - *Dropping* a lens is a RELAXATION and may key ONLY off engine-lane deterministic
      signals: the explicit ``change_class`` (ENGINE-lane git-diff tag) and ``diff_stat``
      (``git diff --numstat``) parameters. They are read from the parameters and are
      deliberately NEVER pulled out of ``context`` inside this function, because ``context``
      is a channel a model writes to — otherwise an implementer that under-reported its diff
      size or claimed "docs-only" could talk itself into a thinner review.

    The ladder, in order:

    1. Deterministically-trivial diff ⇒ ``find:code`` alone (a typo fix should not pay for a
       dedicated spec AND test reviewer, each carrying a full context render).
    2. Otherwise the unrelaxed base panel: ``find:code``, ``find:spec``, ``find:tests``.
    3. ``change_class == "docs-only"`` ⇒ drop ``find:tests`` (the same relaxation, on the
       same trusted tag, the single-reviewer path already makes).
    4. Independently, a frontend change appends ``find:design`` — so a large frontend diff
       resolves to the 4-lens full panel.

    ``agent_for`` is the project's roster resolver; a lens whose sub-role the roster does not
    define resolves to None, which the runner reads as "the base reviewer persona".
    """
    shared = _shared_sections(
        task_id=task_id,
        title=title,
        body=body,
        context=context,
        project_commands=project_commands,
    )

    docs_only = change_class == "docs-only"
    if _is_trivial_diff(diff_stat):
        lenses: list[_Lens] = [_LENS_CODE]
    else:
        lenses = [lens for lens in _BASE_LENSES if not (docs_only and lens is _LENS_TESTS)]
    if _has_frontend_change((context or {}).get("files_changed")):
        lenses.append(_LENS_DESIGN)

    finders: list[FinderSpec] = []
    for lens in lenses:
        instruction = lens.body
        # The docs-only directive is ENGINE-trusted (parameter, not context) and rides every
        # surviving lens so a finder doesn't demand unreachable test coverage.
        if docs_only:
            instruction += _DOCS_ONLY_DIRECTIVE
        if learnings:
            instruction += f"\n\n## Prior attempts (learn from these)\n{learnings}"
        finders.append(
            FinderSpec(
                lens=lens.name,
                prompt="\n\n".join([*shared, instruction]),
                agent=agent_for(Stage.REVIEW, lens.agent_role) if agent_for else None,
                schema_ref=_FINDINGS_SCHEMA_REF,
            )
        )
    return ReviewPlan(
        finders=tuple(finders),
        verify_template=_VERIFY_TEMPLATE,
        verify_schema_ref=_VERDICT_SCHEMA_REF,
        dedupe_rule=_DEDUPE_RULE,
    )
