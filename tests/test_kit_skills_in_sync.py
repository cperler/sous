"""The scaffold kit's skills must never fall behind this repo's own.

The kit (``templates/project-default/skills/``) is what ``orchestrator-scaffold`` seeds
into a NEW project's ``.claude/skills/<name>/SKILL.md``. This repo runs its own copies at
``.claude/skills/<name>/SKILL.md``. Nothing derived one from the other, so for a long while
they silently diverged in the worst direction: the shipped batch-supervisor skill was less
than half the length of the live one and had lost BOTH hard-won guardrails — ``--shared-root``
(a fresh ``runs/`` can't be auto-detected as a shared root, so the store writes flat) and the
"pass the WorkItem through VERBATIM" warning (#311, learned from the live ``batch-next5b``
failure where a truncated ``content_hash`` was refused at ``record``). A project scaffolded
then would have re-learned both the expensive way.

So the kit copies are now byte-identical copies, and this pins them. If you edit a live
skill, copy it across — the whole point is that a scaffolded project gets the CURRENT
generation of the skill, not the one that happened to be current when the kit was written.

Byte-identity is deliberate rather than a normalized comparison: any "allowed difference"
is a place drift can hide again, and these skills are already written project-neutrally
(the project adapter appears as a ``<your-project-adapter>`` placeholder, not a real one).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
KIT_SKILLS = REPO / "templates" / "project-default" / "skills"
LIVE_SKILLS = REPO / ".claude" / "skills"


def _kit_skill_names() -> list[str]:
    return sorted(p.stem for p in KIT_SKILLS.glob("*.md"))


def test_kit_ships_at_least_the_run_lane_skills() -> None:
    """Guards the parametrization below: if the kit is emptied or renamed, the sync test
    must not silently degrade into asserting nothing."""
    names = _kit_skill_names()
    assert {
        "orchestrate-task-interactive",
        "orchestrate-batch-interactive",
        "orchestrate-batch-headless",
        "triage-followups",
    } <= set(names), f"kit skills unexpectedly missing: {names}"


@pytest.mark.parametrize("name", _kit_skill_names())
def test_kit_skill_matches_the_live_skill(name: str) -> None:
    live = LIVE_SKILLS / name / "SKILL.md"
    assert live.is_file(), (
        f"the kit ships {name}.md but this repo has no .claude/skills/{name}/SKILL.md — "
        "either the kit skill is obsolete or the live one was deleted"
    )
    kit_text = (KIT_SKILLS / f"{name}.md").read_text(encoding="utf-8")
    assert kit_text == live.read_text(encoding="utf-8"), (
        f"templates/project-default/skills/{name}.md has drifted from "
        f".claude/skills/{name}/SKILL.md. A scaffolded project would get the older "
        f"generation. Fix by copying the live skill across:\n"
        f"    cp .claude/skills/{name}/SKILL.md templates/project-default/skills/{name}.md"
    )


@pytest.mark.parametrize("name", _kit_skill_names())
def test_kit_skill_has_frontmatter_naming_it(name: str) -> None:
    """``scaffold._skill_slug`` keys the seed directory off the frontmatter ``name:`` and
    falls back to the file stem. The fallback is a safety net, not a plan: a skill with no
    frontmatter registers with its H1 as the description (which is exactly how
    ``orchestrate-batch-headless`` shipped before this test existed)."""
    lines = (KIT_SKILLS / f"{name}.md").read_text(encoding="utf-8").splitlines()
    assert lines and lines[0].strip() == "---", f"{name}.md does not open with frontmatter"
    block = lines[1 : lines.index("---", 1)]
    assert f"name: {name}" in block, f"{name}.md frontmatter does not declare `name: {name}`"
    assert any(line.startswith("description:") for line in block), (
        f"{name}.md frontmatter has no `description:` — Claude Code would fall back to the "
        "H1 heading when listing the skill"
    )
