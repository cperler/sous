"""Per-stage specs + prompt rendering (target.md §6.1).

The engine owns the *generic* stage scaffolding (what each collapsed stage is for,
its model role, its output schema key, its agent sub-role). Project-specific values
(test commands, agent names, taxonomy) come from the project-config adapter — so the
prompts stay repo-agnostic and the same engine drives any project.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model_table import Role
from .schemas.enums import STAGE_ORDER, Stage


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


# The 6 collapsed stages. Templates are deliberately terse, goal-plus-constraints
# (newer models do better with that than enumerated micro-steps — design-doc §2).
STAGE_SPECS: dict[Stage, StageSpec] = {
    Stage.INTAKE: StageSpec(
        stage=Stage.INTAKE,
        model_role=Role.CHEAP_SHELL,
        schema_ref="intake",
        agent_role=None,
        timeout_s=300,  # cheap shell: worktree prep + baseline
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
        template=(
            "Implement the change and commit it. Follow the scope plan in the context "
            "above if present; if none (lite/micro), implement the task spec directly "
            "as a single change.\n"
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
        template=(
            "Run the project's tests for the changed files, fix regressions you "
            "introduced (not inherited failures), and re-run until green or no "
            "progress. Then VERIFY the tests are meaningful: they must exercise THIS "
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
        template=(
            "Add/refresh docstrings for changed source, then open a pull request for "
            "the task branch. If the context above already shows a pr_url for this "
            "task (a review fix cycle), push the branch and reuse that PR — never "
            "open a duplicate.\n"
            "Return: pr_number, pr_url."
        ),
    ),
    Stage.REVIEW: StageSpec(
        stage=Stage.REVIEW,
        model_role=Role.REVIEW,
        schema_ref="review",
        agent_role="review",
        timeout_s=600,  # read the PR + judge
        template=(
            "Review the PR (see pr_url in the context above) against the task goal and "
            "code quality. Assess the goal criterion-by-criterion and check for "
            "regressions; approve only if it achieves the goal without regressions. "
            "Separately, record any NON-BLOCKING findings (nits, edge cases, polish, "
            "follow-on ideas) that should be tracked but must not hold up this PR — the "
            "engine files each as a deferred-scope follow-up issue at finalize, so "
            "nothing you notice is silently dropped.\n"
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
            "auto-approve), non_blocking (list of {title, detail}; empty if none), "
            "improvement ({title, detail} or omitted), retrospective ({title, detail} or "
            "omitted)."
        ),
    ),
}


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

    instruction = f"## {stage.value.upper()}\n{spec.template}"
    if learnings:
        instruction += f"\n\n## Prior attempts (learn from these)\n{learnings}"
    parts.append(instruction)

    return "\n\n".join(parts)
