"""Integrity of the default project starter kit (templates/project-default/).

Keeps the manifest, the asset files, and the engine's stage contracts in lock-step so a
future edit can't leave a dangling reference that the bootstrap would seed broken.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from jsonschema import Draft202012Validator

from orchestrator.scaffold import _skill_slug
from orchestrator.schemas.stage_schemas import load_stage_schema

KIT = Path(__file__).resolve().parent.parent / "templates" / "project-default"
STAGE_REFS = ("intake", "scope", "implement", "test", "deliver", "review")


def _manifest() -> dict:
    return tomllib.loads((KIT / "manifest.toml").read_text())


def test_manifest_agents_are_backed_and_named() -> None:
    for agent, spec in _manifest()["agents"].items():
        path = KIT / "agents" / f"{agent}.md"
        assert path.exists(), f"manifest names agent {agent} with no agents/{agent}.md"
        head = path.read_text().splitlines()
        assert head[0] == "---" and f"name: {agent}" in head, f"{agent}.md frontmatter name mismatch"
        assert isinstance(spec.get("roles"), list) and spec["roles"], f"{agent} has no roles"


def test_manifest_hooks_and_skills_resolve() -> None:
    m = _manifest()
    for hook in m["hooks"]:
        p = KIT / "hooks" / f"{hook}.json"
        assert p.exists(), f"missing hooks/{hook}.json"
        json.loads(p.read_text())  # valid JSON
    for skill in m["skills"]["always"]:
        src = KIT / "skills" / f"{skill}.md"
        assert src.exists(), f"missing skills/{skill}.md"
        # Every kit skill must carry a frontmatter name — that name becomes the
        # .claude/skills/<name>/SKILL.md dir the bootstrap seeds (the invocable slash
        # command), so a missing/blank name would seed an undiscoverable skill.
        slug = _skill_slug(src)
        assert slug and slug != src.stem, f"{skill}.md has no frontmatter name:"


def test_kit_schemas_match_canonical() -> None:
    # The seeded schema copies must equal the engine's canonical contracts (codex validation).
    for ref in STAGE_REFS:
        seeded = json.loads((KIT / "schemas" / f"{ref}.json").read_text())
        assert seeded == load_stage_schema(ref), f"{ref}.json drifted from the canonical schema"
        Draft202012Validator.check_schema(seeded)


def test_default_roster_is_backed_by_kit_agents() -> None:
    # Every agent the scaffold's no-stack default roster references exists in the kit, and
    # every role it maps is declared by that agent in the manifest.
    from orchestrator.scaffold import _DEFAULT_ROSTER

    agents = _manifest()["agents"]
    for role, agent in _DEFAULT_ROSTER.items():
        assert agent in agents, f"default roster references unknown kit agent {agent}"
        assert role in agents[agent]["roles"], f"{agent} can't serve {role}"
