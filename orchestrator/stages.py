"""Per-stage specs + prompt rendering (target.md §6.1).

The engine owns the *generic* stage scaffolding (what each collapsed stage is for,
its model role, its output schema key, its agent sub-role). Project-specific values
(test commands, agent names, taxonomy) come from the project-config adapter — so the
prompts stay repo-agnostic and the same engine drives any project.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model_table import Role
from .schemas.enums import Stage


@dataclass(frozen=True)
class StageSpec:
    stage: Stage
    model_role: str  # resolved to a model id by the model_table
    schema_ref: str  # output-schema key the runner enforces
    agent_role: str | None  # sub-role for ProjectConfig.agent_for()
    template: str  # prompt template; {placeholders} filled at render time


# The 6 collapsed stages. Templates are deliberately terse, goal-plus-constraints
# (newer models do better with that than enumerated micro-steps — design-doc §2).
STAGE_SPECS: dict[Stage, StageSpec] = {
    Stage.INTAKE: StageSpec(
        stage=Stage.INTAKE,
        model_role=Role.CHEAP_SHELL,
        schema_ref="intake",
        agent_role=None,
        template=(
            "INTAKE for task {task_id} ({title}).\n"
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
        template=(
            "SCOPE for task {task_id} ({title}).\n{body}\n\n"
            "Understand the change, decide feasibility, and produce a minimal task "
            "plan. If genuinely blocked, say so.\n{learnings}"
            "Return: feasible, blocked_reason, plan (list of subtasks)."
        ),
    ),
    Stage.IMPLEMENT: StageSpec(
        stage=Stage.IMPLEMENT,
        model_role=Role.DEEP_REASON,
        schema_ref="implement",
        agent_role="implement",
        template=(
            "IMPLEMENT for task {task_id} ({title}).\n"
            "Implement the planned change and commit it. If no scope plan is present "
            "(lite/micro), implement the task spec directly as a single change.\n"
            "{learnings}"
            "Return: files_changed, summary, committed."
        ),
    ),
    Stage.TEST: StageSpec(
        stage=Stage.TEST,
        model_role=Role.REVIEW,
        schema_ref="test",
        agent_role="test",
        template=(
            "TEST for task {task_id} ({title}).\n"
            "Run the project's tests for the changed files, fix regressions you "
            "introduced (not inherited failures), and re-run until green or no "
            "progress. Then VERIFY the tests are meaningful: they must exercise THIS "
            "change and would fail if it regressed — not vacuous, tautological, or "
            "always-green.\n{learnings}"
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
        template=(
            "DELIVER for task {task_id} ({title}).\n"
            "Add/refresh docstrings for changed source, then open a pull request for "
            "the task branch.\n"
            "Return: pr_number, pr_url."
        ),
    ),
    Stage.REVIEW: StageSpec(
        stage=Stage.REVIEW,
        model_role=Role.REVIEW,
        schema_ref="review",
        agent_role="review",
        template=(
            "REVIEW for task {task_id} ({title}).\n"
            "Review the PR against the task goal and code quality. Approve only if it "
            "achieves the goal without regressions.\n{learnings}"
            "Return: approved, issues (list)."
        ),
    ),
}


def render_prompt(stage: Stage, *, task_id: str, title: str, body: str, learnings: str = "") -> str:
    """Fill a stage template. ``learnings`` is the appended prior-attempt context."""
    spec = STAGE_SPECS[stage]
    learn_block = f"Prior attempts (learn from these):\n{learnings}\n\n" if learnings else ""
    return spec.template.format(
        task_id=task_id,
        title=title or "(no title)",
        body=(body or "").strip(),
        learnings=learn_block,
    )
