"""Adapter bootstrap (target.md §5 — ports `adapting-claude-pipeline`).

Generates a WORKING project-config adapter skeleton for a new repo: an importable
package that satisfies the ProjectConfig protocol out of the box (sensible no-op
defaults + a local task source + a minimal classifier), with ``# TODO`` markers for
the Keep/Modify/Replace/Delete audit a human (or a subagent) fills in. The point is
that standing up a new project means writing an adapter — never touching the engine.
"""

from __future__ import annotations

from pathlib import Path


def _class_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.replace("-", "_").split("_")) + "Config"


_INIT = '''"""{name} project-config adapter (generated skeleton)."""

from __future__ import annotations

from .config import {cls}, get_config

__all__ = ["{cls}", "get_config"]
'''

_CONFIG = '''"""{name} project-config adapter (GENERATED — fill in the TODOs).

Keep / Modify / Replace / Delete audit (the adapting-claude-pipeline workflow):
  - Keep    the structure (the ProjectConfig surface the engine depends on).
  - Modify  the commands + roster for THIS project.
  - Replace the classifier taxonomy + task source if the project differs.
  - Delete  any stage role you do not use.
The engine is never edited — only this adapter.
"""

from __future__ import annotations

import os

from orchestrator.schemas.enums import Stage
from orchestrator.schemas.stage_schemas import resolve_stage_schema

from .classifier import {cls}Classifier
from .task_source import LocalTaskSource

# Seeded stage-output schemas (codex full-validation). Override a stage by dropping a
# <ref>.json here; otherwise the engine's canonical contract is used.
_SCHEMA_DIR = ".claude/schemas"

_NOOP = ["true"]

# Stage (sub-)role -> agent name. Defaults reference the generic starter-kit agents
# (templates/project-default/agents/); the bootstrap swaps in stack-specific implement
# agents (e.g. python-backend-developer) per the project profile.
_ROSTER: dict[str, str] = {{
    "implement": "generic-implementer",
    "test": "test-validator",
    "review": "code-reviewer",
    "review:spec": "spec-reviewer",
    "docstring": "docstring-writer",
}}


class {cls}:
    name = "{name}"

    def __init__(self, tasks_path: str = "tasks.json") -> None:
        self._classifier = {cls}Classifier()
        self._task_source = LocalTaskSource(tasks_path)

    # TODO: replace the no-ops with this project's real commands.
    def install_cmd(self) -> list[str]:
        return _NOOP

    def test_unit_cmd(self, files: list[str] | None = None) -> list[str]:
        # FAIL-CLOSED: errors loudly until you set this, so a run before the TODOs
        # are filled does not vacuously pass. Replace with e.g. ["uv","run","pytest","-q"].
        return ["sh", "-c", "echo 'orchestrator: set {name} test_unit_cmd' >&2; exit 1"]

    def test_e2e_cmd(self, files: list[str] | None = None) -> list[str]:
        return _NOOP

    def test_shell_cmd(self, files: list[str] | None = None) -> list[str]:
        return _NOOP

    def typecheck_cmd(self) -> list[str]:
        return _NOOP

    def infra_reset(self) -> list[str]:
        return _NOOP

    @property
    def classifier(self) -> {cls}Classifier:
        return self._classifier

    @property
    def task_source(self) -> LocalTaskSource:
        return self._task_source

    def agent_for(self, stage: Stage, role: str | None = None) -> str | None:
        return _ROSTER.get(role) if role else None

    def schema_for(self, ref: str) -> dict | None:
        return resolve_stage_schema(ref, local_dir=_SCHEMA_DIR)


def get_config() -> {cls}:
    return {cls}(tasks_path=os.environ.get("{env}", "tasks.json"))
'''

_CLASSIFIER = '''"""{name} failure classifier (GENERATED — adjust the taxonomy)."""

from __future__ import annotations

import re

from orchestrator.failure_classifier import Failure
from orchestrator.schemas.enums import FailureKind

# TODO: tune these patterns + the impacted-tests mapping for this project.
_FAILED = re.compile(r"^FAILED\\s+(\\S+)", re.MULTILINE)


class {cls}Classifier:
    def classify(self, test_output: str) -> list[Failure]:
        return [Failure(test=m.group(1), kind=FailureKind.UNIT) for m in _FAILED.finditer(test_output)]

    def impacted_tests(self, changed_files: list[str]) -> list[str]:
        return [f for f in changed_files if "test" in f]
'''

_TASK_SOURCE = '''"""{name} task source (GENERATED — a local JSON file source by default)."""

from __future__ import annotations

import json
from pathlib import Path

from adapters.project.base import TaskSpec
from orchestrator.errors import OrchestratorError


class LocalTaskSource:
    """Tasks from a JSON file: {{"<id>": {{"title", "body", "depends_on"}}}}."""

    def __init__(self, tasks_path: str | Path) -> None:
        self.tasks_path = Path(tasks_path)

    def resolve(self, task_id: str) -> TaskSpec:
        if not self.tasks_path.exists():
            raise OrchestratorError(f"tasks file not found: {{self.tasks_path}}")
        data = json.loads(self.tasks_path.read_text())
        if task_id not in data:
            raise OrchestratorError(f"unknown task {{task_id!r}}")
        t = data[task_id]
        return TaskSpec(task_id=task_id, title=t.get("title", ""), body=t.get("body", ""),
                        depends_on=list(t.get("depends_on", [])))

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None:
        with open(self.tasks_path.with_name("completed.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{{task_id}}\\t{{pr_url or ''}}\\n")
'''


def scaffold_adapter(name: str, dest_dir: str | Path) -> Path:
    """Write a working adapter skeleton at ``<dest_dir>/<name>/``; return that path."""
    cls = _class_name(name)
    env = f"{name.replace('-', '_').upper()}_TASKS"
    pkg = Path(dest_dir) / name.replace("-", "_")
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(_INIT.format(name=name, cls=cls))
    (pkg / "config.py").write_text(_CONFIG.format(name=name, cls=cls, env=env))
    (pkg / "classifier.py").write_text(_CLASSIFIER.format(name=name, cls=cls))
    (pkg / "task_source.py").write_text(_TASK_SOURCE.format(name=name))
    return pkg


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI shim
    import argparse

    p = argparse.ArgumentParser(prog="orchestrator-scaffold",
                                description="Generate a project-config adapter skeleton.")
    p.add_argument("--name", required=True, help="adapter/project name (e.g. my-service)")
    p.add_argument("--dest", default="adapters/project", help="destination dir for the package")
    args = p.parse_args(argv)
    path = scaffold_adapter(args.name, args.dest)
    print(f"scaffolded adapter: {path}")
    print("Next: fill in the TODOs (commands, taxonomy, roster) per the Keep/Modify/Replace/Delete audit.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
