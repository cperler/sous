"""Per-stage specs + prompt rendering (target.md §6.1).

The engine owns the *generic* stage scaffolding (what each collapsed stage is for,
its model role, its output schema key, its agent sub-role). Project-specific values
(test commands, agent names, taxonomy) come from the project-config adapter — so the
prompts stay repo-agnostic and the same engine drives any project.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model_table import Role
from .schemas.enums import STAGE_ORDER, Effort, Stage


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
            "needs its own design) OR non-trivial (real work/risk). `fix_now` for a "
            "trivial nit inside the change's blast radius the implementer should have "
            "absorbed in place (a docstring its edit made stale, a guard/type on a touched "
            "line) — do NOT inflate these into tickets. `drop` for a real-but-untracked "
            "observation. The engine files only `file` findings (up to a small per-task "
            "cap) and surfaces the rest in the completion note, so nothing is silently "
            "dropped without ballooning the backlog. Do NOT restate one idea as both a "
            "non_blocking finding and the `improvement` below — pick one.\n"
            "Finally — the self-improvement loop — step back from THIS PR and propose: "
            "(a) improvement — the single highest-value forward-looking enhancement this "
            "task suggests for the PROJECT/roadmap (filed as an enhancement issue), and "
            "(b) retrospective — one lesson this task teaches about the ORCHESTRATION "
            "PROCESS itself (prompts, stages, tooling, lanes). Omit either if nothing "
            "genuine stands out — do not pad.\n"
            "Return: approved, issues (blocking; empty when approved — each an object "
            "{severity: critical|important|suggestion, file, line, description, "
            "suggested_fix}; a rejection re-runs implement→…→review with your issues as "
            "learnings, so make them concrete and actionable; suggestion-only rejections "
            "auto-approve), non_blocking (list of {title, detail, disposition: "
            "file|fix_now|drop}; empty if none), improvement ({title, detail} or omitted), "
            "retrospective ({title, detail} or omitted)."
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


# Project-agnostic design-review criteria injected into the REVIEW prompt when the change
# touches frontend files (#62). The heysoo-specific design-system tokens (visual language,
# component library, theme rules) stay in the adapter's design agent — this block is the
# reusable craft lens only.
_DESIGN_REVIEW_LENS = (
    "\n\n## Frontend change: apply the design-review lens\n"
    "This change touches user-facing files (per files_changed above). Beyond correctness, "
    "review the design craft:\n"
    "- Visual hierarchy: size/weight/spacing guide attention; the primary element reads first.\n"
    "- Spacing & alignment: a consistent scale (e.g. an 8pt grid), no arbitrary one-off values.\n"
    "- Consistency & reuse: reuse existing components/patterns/tokens over reinventing them.\n"
    "- Accessibility: sufficient contrast, keyboard operability, visible focus, labels/roles, "
    "adequate tap targets; never rely on color alone.\n"
    "- Responsive behavior: works across viewport sizes and larger text; no fixed heights on "
    "text containers; graceful with more/less content.\n"
    "Treat these as review criteria — blocking only when a change materially harms usability "
    "or accessibility; otherwise record them as non-blocking polish."
)


def _render_value(v: object) -> str:
    """One folded context value → a compact one-line-ish string for the prompt."""
    if isinstance(v, list):
        return "; ".join(str(x) for x in v) if v else "(none)"
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

    - REVIEW (all tasks, #168): instructs the reviewer to OMIT ``tests_meaningful``
      rather than answer ``false`` when there is no test surface to judge — belt-and-
      suspenders with the engine's fail-open-on-omit behaviour in ``_review_verdict``.
      A literal ``false`` on a no-test-surface change spuriously triggers the #13
      independent-test-validate reject.

    - REVIEW + frontend change (#62): appends the design-review lens when folded context
      signals a frontend file was changed.
    """
    spec = STAGE_SPECS[stage]
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

    instruction = f"## {stage.value.upper()}\n{spec.template}"
    # #41: a deterministically-tagged docs-only change has no behavioral surface. Tell the
    # TEST/REVIEW stages to skip test-coverage / tests_meaningful criteria for it, so the
    # reviewer doesn't demand (or reject over) unreachable coverage. The tag is set only by
    # the ENGINE-lane git diff, so this directive can't be triggered by a model's own claim.
    if stage in (Stage.TEST, Stage.REVIEW) and (context or {}).get("change_class") == "docs-only":
        instruction += (
            "\n\n## Change classification: DOCS-ONLY\n"
            "This change was deterministically classified as documentation-only (every "
            "changed file is docs). It has no behavioral surface, so DO NOT apply "
            "test-coverage criteria: treat tests_meaningful as satisfied and do not reject "
            "or hold this change for lacking new/updated tests. Judge it on documentation "
            "correctness and clarity instead."
        )
    # #168: belt-and-suspenders with the engine's fail-open-on-omit gate — tell the reviewer
    # to OMIT `tests_meaningful` (rather than answer `false`) when there is no test surface to
    # judge (a docs/config change, or a task whose pipeline runs no meaningful tests). A literal
    # `false` on a no-test-surface change is what spuriously kicked #144 into a fix cycle.
    if stage is Stage.REVIEW:
        instruction += (
            "\n\n## Reporting tests_meaningful\n"
            "Only set `tests_meaningful` when there ARE tests to judge. If this change has no "
            "test surface (e.g. a docs/config-only change, or nothing whose behavior tests "
            "could exercise), OMIT the field entirely rather than answering `false` — a literal "
            "`false` reads as a rejection for lacking meaningful tests."
        )
    # #62: a frontend change (deterministic signal on files_changed folded from IMPLEMENT)
    # gets the design-review criteria block appended to the REVIEW prompt. Project-agnostic
    # wording; the heysoo-specific design tokens live in the adapter's design agent.
    if stage is Stage.REVIEW and _has_frontend_change((context or {}).get("files_changed")):
        instruction += _DESIGN_REVIEW_LENS
    if learnings:
        instruction += f"\n\n## Prior attempts (learn from these)\n{learnings}"
    parts.append(instruction)

    return "\n\n".join(parts)
