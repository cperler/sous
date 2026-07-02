"""Project bootstrap — profile-driven adapter generation + starter-kit seeding (§5).

Standing up a new project for orchestration means writing ONE project-config adapter
(the engine is never touched). This module generates that adapter from a **profile**
(the project's stack + commands + roster + layers) and seeds the stack-appropriate
subset of the starter kit (``templates/project-default/``) into the new project's
``.claude/``. It is deterministic ("profile in -> files out"), idempotent, and
*additive* on re-run (it extends a project without clobbering hand-edits).

The interactive interview that *composes* a profile (detect stack -> ask -> tune) is a
separate run-target skill; this is the deterministic layer it drives.
"""

from __future__ import annotations

import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from adapters.project.base import ADAPTER_CONTRACT_VERSION

KIT_DIR = Path(__file__).resolve().parent.parent / "templates" / "project-default"

# Command method <-> profile key. Order is the order they appear in the generated config.
_COMMAND_METHODS: list[tuple[str, str, bool]] = [
    # (method_name, profile_key, takes_files_arg)
    ("install_cmd", "install", False),
    ("test_unit_cmd", "test_unit", True),
    ("test_e2e_cmd", "test_e2e", True),
    ("test_shell_cmd", "test_shell", True),
    ("typecheck_cmd", "typecheck", False),
    ("infra_reset", "infra_reset", False),
]

# The generic roster used when no stack-specific agents are selected (no-profile default).
_DEFAULT_ROSTER: dict[str, str] = {
    "implement": "generic-implementer",
    "test": "test-validator",
    "review": "code-reviewer",
    "review:spec": "spec-reviewer",
    "docstring": "docstring-writer",
}


# ---------------------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------------------

@dataclass
class Profile:
    """The persisted source of truth for a project's adapter (``profile.toml``)."""

    name: str
    languages: list[str] = field(default_factory=list)
    task_source: str = "local-file"
    commands: dict[str, list[str]] = field(default_factory=dict)
    roster: dict[str, str] = field(default_factory=dict)
    layers: dict[str, bool] = field(default_factory=dict)
    seed: dict[str, list[str]] = field(default_factory=dict)
    # The adapter-contract version the generated files target (checked at load for
    # project-owned adapters). (Re)generation always writes the engine's current one.
    contract_version: int = ADAPTER_CONTRACT_VERSION


def load_kit_manifest() -> dict:
    """Parse the starter-kit menu (agents/hooks tags, skills, stack->commands)."""
    return tomllib.loads((KIT_DIR / "manifest.toml").read_text(encoding="utf-8"))


def select_kit_assets(profile: Profile, manifest: dict) -> dict[str, list[str]]:
    """Which kit assets this profile rolls in: generic-always + per-language tagged."""
    langs = set(profile.languages)
    agents = [
        a for a, spec in manifest["agents"].items()
        if not spec.get("tags") or langs.intersection(spec["tags"])
    ]
    hooks = [
        h for h, spec in manifest["hooks"].items() if langs.intersection(spec.get("tags", []))
    ]
    skills = list(manifest.get("skills", {}).get("always", []))
    return {"agents": sorted(agents), "hooks": sorted(hooks), "skills": skills}


def profile_from_languages(name: str, languages: list[str], manifest: dict) -> Profile:
    """Synthesize a default profile for a stack from the kit manifest."""
    langs = [lower for lang in languages if (lower := lang.strip().lower())]
    selected = select_kit_assets(Profile(name=name, languages=langs), manifest)

    # Roster: each selected agent claims its declared roles. Process generics first so a
    # stack-specific agent (e.g. python-backend-developer) wins the shared role (implement).
    agents = manifest["agents"]
    generics = [a for a in selected["agents"] if not agents[a].get("tags")]
    stack = [a for a in selected["agents"] if agents[a].get("tags")]
    roster: dict[str, str] = {}
    for agent in [*generics, *stack]:
        for role in agents[agent].get("roles", []):
            roster[role] = agent

    # Commands: union the per-language defaults from the manifest's [commands.<lang>] tables.
    commands: dict[str, list[str]] = {}
    for lang in langs:
        for key, argv in manifest.get("commands", {}).get(lang, {}).items():
            commands.setdefault(key, list(argv))

    profile = Profile(name=name, languages=langs, commands=commands, roster=roster)
    profile.seed = select_kit_assets(profile, manifest)
    return profile


def _overrides(profile: Profile, manifest: dict) -> tuple[dict[str, list[str]], dict[str, str]]:
    """A profile's HAND-edits: the commands/roster entries that differ from what
    profile_from_languages would derive as defaults for its own languages. This lets a
    re-run keep human overrides without a fully-defaulted incoming profile clobbering them."""
    base = profile_from_languages(profile.name, profile.languages, manifest)
    cmd = {k: v for k, v in profile.commands.items() if base.commands.get(k) != v}
    roster = {k: v for k, v in profile.roster.items() if base.roster.get(k) != v}
    return cmd, roster


def merge_profiles(existing: Profile, incoming: Profile, manifest: dict) -> Profile:
    """Additive merge for a re-run: union the languages, re-derive defaults for the union,
    then re-apply hand-overrides (incoming's win over existing's). Nothing is removed."""
    languages = list(dict.fromkeys([*existing.languages, *incoming.languages]))
    merged = profile_from_languages(existing.name, languages, manifest)
    merged.task_source = incoming.task_source or existing.task_source
    ex_cmd, ex_roster = _overrides(existing, manifest)
    in_cmd, in_roster = _overrides(incoming, manifest)
    merged.commands.update({**ex_cmd, **in_cmd})
    merged.roster.update({**ex_roster, **in_roster})
    merged.layers = {**existing.layers, **incoming.layers}
    merged.seed = select_kit_assets(merged, manifest)
    return merged


# ---------------------------------------------------------------------------------------
# Stack detection (the deterministic first-guess the interview skill presents)
# ---------------------------------------------------------------------------------------

# Package-manager command sets, keyed by lockfile-detected PM. These OVERRIDE the
# manifest's per-language defaults (a lockfile is stronger evidence than the default PM).
_PM_COMMANDS: dict[str, dict[str, list[str]]] = {
    "poetry": {"install": ["poetry", "install"], "test_unit": ["poetry", "run", "pytest", "-q"],
               "typecheck": ["poetry", "run", "mypy", "."]},
    "pip": {"install": ["pip", "install", "-r", "requirements.txt"],
            "test_unit": ["python", "-m", "pytest", "-q"]},
    "pnpm": {"install": ["pnpm", "install"], "test_unit": ["pnpm", "test"],
             "typecheck": ["pnpm", "exec", "tsc", "--noEmit"],
             "test_e2e": ["pnpm", "exec", "playwright", "test"]},
    "yarn": {"install": ["yarn", "install"], "test_unit": ["yarn", "test"],
             "typecheck": ["yarn", "tsc", "--noEmit"], "test_e2e": ["yarn", "playwright", "test"]},
    "npm": {"install": ["npm", "ci"], "test_unit": ["npm", "test"],
            "typecheck": ["npm", "exec", "tsc", "--noEmit"],
            "test_e2e": ["npm", "exec", "playwright", "test"]},
}


def _detect_languages(root: Path) -> list[str]:
    langs: list[str] = []
    if any((root / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")):
        langs.append("python")
    pkg = root / "package.json"
    if (root / "tsconfig.json").exists() or (pkg.exists() and "typescript" in pkg.read_text(errors="ignore")):
        langs.append("typescript")
    elif pkg.exists():
        langs.append("node")
    if (root / "go.mod").exists():
        langs.append("go")
    if (root / "Cargo.toml").exists():
        langs.append("rust")
    return langs


def _python_pm(root: Path) -> str:
    if (root / "poetry.lock").exists():
        return "poetry"
    if (root / "requirements.txt").exists() and not (root / "pyproject.toml").exists():
        return "pip"
    return "uv"  # uv.lock or pyproject default — manifest default, no override


def _js_pm(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _has_playwright(root: Path) -> bool:
    if any(root.glob("playwright.config.*")):
        return True
    pkg = root / "package.json"
    return pkg.exists() and "playwright" in pkg.read_text(errors="ignore")


def _detect_commands(root: Path, languages: list[str], manifest: dict) -> dict[str, list[str]]:
    """Per-language commands (manifest default refined by the detected package manager),
    merged in language order so the FIRST language wins genuinely-shared keys (test_unit)."""
    cmds: dict[str, list[str]] = {}
    for lang in languages:
        lang_cmds = {k: list(v) for k, v in manifest.get("commands", {}).get(lang, {}).items()}
        pm = _python_pm(root) if lang == "python" else _js_pm(root) if lang in ("typescript", "node") else None
        if pm and pm in _PM_COMMANDS:
            lang_cmds.update({k: list(v) for k, v in _PM_COMMANDS[pm].items()})
        for k, v in lang_cmds.items():
            cmds.setdefault(k, v)  # first language claims a shared key
    if not _has_playwright(root):
        cmds.pop("test_e2e", None)  # only keep an e2e command if the project actually has e2e
    return cmds


def _detect_task_source(root: Path) -> str:
    cfg = root / ".git" / "config"
    if cfg.exists() and "github.com" in cfg.read_text(errors="ignore"):
        return "github-issues"
    return "local-file"


def detect_profile(repo_root: str | Path, name: str, manifest: dict) -> Profile:
    """Deterministic stack detection -> a draft Profile the interview presents for confirmation.

    Reuses ``profile_from_languages`` for roster/seed, then refines commands from lockfiles
    and guesses the task source. The human corrects this draft (detect-then-confirm)."""
    root = Path(repo_root)
    languages = _detect_languages(root)
    profile = profile_from_languages(name, languages, manifest)
    profile.commands = _detect_commands(root, languages, manifest)
    profile.task_source = _detect_task_source(root)
    return profile


# ---------------------------------------------------------------------------------------
# TOML (write) — tiny serializer for the constrained profile shape (stdlib has no writer)
# ---------------------------------------------------------------------------------------

def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_key(k: str) -> str:
    bare = k.replace("_", "").replace("-", "")
    return k if bare.isalnum() and not k[:1].isdigit() else _toml_str(k)


def _toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"unsupported TOML value: {v!r}")


def profile_to_toml(profile: Profile) -> str:
    """Serialize a Profile to TOML text (dependency-free; read back with tomllib)."""
    lines = [
        "# Generated by orchestrator-scaffold; the source of truth for this adapter.",
        "# Edit here (or re-run the bootstrap) — config.py is regenerated from this file.",
        "",
        "[project]",
        f"name = {_toml_str(profile.name)}",
        f"languages = {_toml_value(profile.languages)}",
        f"task_source = {_toml_str(profile.task_source)}",
        f"contract_version = {profile.contract_version}",
    ]
    for section, data in (
        ("commands", profile.commands), ("roster", profile.roster),
        ("layers", profile.layers), ("seed", profile.seed),
    ):
        if data:
            lines += ["", f"[{section}]"]
            lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in data.items()]
    return "\n".join(lines) + "\n"


def write_profile(profile: Profile, path: Path) -> None:
    path.write_text(profile_to_toml(profile), encoding="utf-8")


def read_profile(path: Path) -> Profile:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    proj = raw.get("project", {})
    return Profile(
        name=proj.get("name", path.parent.name),
        languages=list(proj.get("languages", [])),
        task_source=proj.get("task_source", "local-file"),
        commands={k: list(v) for k, v in raw.get("commands", {}).items()},
        roster=dict(raw.get("roster", {})),
        layers=dict(raw.get("layers", {})),
        seed={k: list(v) for k, v in raw.get("seed", {}).items()},
        contract_version=int(proj.get("contract_version", ADAPTER_CONTRACT_VERSION)),
    )


# ---------------------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------------------

def _class_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.replace("-", "_").split("_")) + "Config"


def _env_var(name: str) -> str:
    return f"{name.replace('-', '_').upper()}_TASKS"


def _argv_literal(argv: list[str]) -> str:
    return "[" + ", ".join(repr(a) for a in argv) + "]"


_INIT = '''"""{name} project-config adapter (generated skeleton)."""

from __future__ import annotations

from .config import {cls}, get_config

# The ProjectConfig contract this adapter was generated against (checked at load
# when the adapter lives outside the engine repo). Regenerating updates it.
CONTRACT_VERSION = {contract}

__all__ = ["{cls}", "get_config"]
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

_TASK_SOURCE = '''"""{name} task source (GENERATED — a local JSON file source by default).

If the project's profile says ``task_source = "github-issues"``, replace this with a
GitHub-Issues source (see ``adapters/project/heysoo/task_source.py`` for the shape).
"""

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


def _command_method(method: str, key: str, takes_files: bool, profile: Profile, name: str) -> str:
    sig_arg = "self, files: list[str] | None = None" if takes_files else "self"
    argv = profile.commands.get(key)
    if argv:
        body = f"        return {_argv_literal(argv)}"
    elif key == "test_unit":
        # FAIL-CLOSED until set, so an unconfigured run can't vacuously pass.
        body = (f"        return [\"sh\", \"-c\", "
                f"\"echo 'orchestrator: set {name} test_unit_cmd' >&2; exit 1\"]")
    else:
        body = "        return _NOOP"
    return f"    def {method}({sig_arg}) -> list[str]:\n{body}"


def render_config(name: str, profile: Profile) -> str:
    """Render config.py as a generated VIEW of the profile (commands + roster baked in)."""
    cls, env = _class_name(name), _env_var(name)
    roster_lines = "".join(f"    {k!r}: {v!r},\n" for k, v in (profile.roster or _DEFAULT_ROSTER).items())
    commands = "\n\n".join(
        _command_method(m, k, f, profile, name) for m, k, f in _COMMAND_METHODS
    )
    return f'''"""{name} project-config adapter (GENERATED from profile.toml — do not hand-edit).

Edit profile.toml (or re-run orchestrator-scaffold) and this file is regenerated.
The engine is never edited — only this adapter. classifier.py and task_source.py are
written once and are yours to tune.
"""

from __future__ import annotations

import os
from pathlib import Path

from orchestrator.schemas.enums import Stage
from orchestrator.schemas.stage_schemas import resolve_stage_schema

from .classifier import {cls}Classifier
from .task_source import LocalTaskSource

_NOOP = ["true"]

# Optional per-stage schema overrides live next to this file (CWD-independent); absent
# a <ref>.json the engine's canonical stage contract is used (codex full-validation).
_SCHEMA_DIR = Path(__file__).parent / "schemas"

# Stage (sub-)role -> agent name (from the starter kit), baked from profile.toml [roster].
_ROSTER: dict[str, str] = {{
{roster_lines}}}


class {cls}:
    name = "{name}"

    def __init__(self, tasks_path: str = "tasks.json") -> None:
        self._classifier = {cls}Classifier()
        self._task_source = LocalTaskSource(tasks_path)

{commands}

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


# ---------------------------------------------------------------------------------------
# Seeding + orchestration
# ---------------------------------------------------------------------------------------

def seed_kit(assets: dict[str, list[str]], into: Path) -> list[str]:
    """Copy the selected kit assets into ``<into>/.claude/`` (agents, skills, hooks)."""
    claude = Path(into) / ".claude"
    seeded: list[str] = []
    for agent in assets.get("agents", []):
        dst = claude / "agents" / f"{agent}.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(KIT_DIR / "agents" / f"{agent}.md", dst)
        seeded.append(f"agents/{agent}.md")
    for skill in assets.get("skills", []):
        dst = claude / "skills" / f"{skill}.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(KIT_DIR / "skills" / f"{skill}.md", dst)
        seeded.append(f"skills/{skill}.md")
    for hook in assets.get("hooks", []):
        dst = claude / "hooks" / f"{hook}.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(KIT_DIR / "hooks" / f"{hook}.json", dst)
        seeded.append(f"hooks/{hook}.json")
    return seeded


def scaffold_adapter(
    name: str,
    dest_dir: str | Path | None = None,
    *,
    profile: Profile | None = None,
    into: str | Path | None = None,
    package_dir: str | Path | None = None,
) -> Path:
    """Generate (or additively update) a project adapter at ``<dest_dir>/<name>/``.

    - No ``profile`` reproduces the generic no-stack skeleton (backward-compatible).
    - If a ``profile.toml`` already exists, the incoming profile is merged additively and
      config.py + profile.toml regenerated; classifier.py / task_source.py are left alone.
    - ``into`` seeds the stack's kit assets into that project root's ``.claude/``.
    - ``package_dir`` writes the package files directly into that dir instead of
      ``<dest_dir>/<name>/`` — the project-owned layout (``<repo>/.orchestration/``),
      loadable by path via ``orchestrator --project <package_dir>``.
    """
    if package_dir is None and dest_dir is None:
        raise ValueError("scaffold_adapter needs dest_dir or package_dir")
    manifest = load_kit_manifest()
    pkg = Path(package_dir) if package_dir is not None else Path(dest_dir) / name.replace("-", "_")
    pkg.mkdir(parents=True, exist_ok=True)
    cls = _class_name(name)

    incoming = profile or profile_from_languages(name, [], manifest)
    profile_path = pkg / "profile.toml"
    if profile_path.exists():
        incoming = merge_profiles(read_profile(profile_path), incoming, manifest)
    else:
        incoming.seed = select_kit_assets(incoming, manifest)

    # Generated-from-profile (always (re)written).
    write_profile(incoming, profile_path)
    (pkg / "__init__.py").write_text(
        _INIT.format(name=name, cls=cls, contract=ADAPTER_CONTRACT_VERSION)
    )
    (pkg / "config.py").write_text(render_config(name, incoming))
    # Hand-editable (written once; never clobber on re-run).
    for fname, template in (("classifier.py", _CLASSIFIER), ("task_source.py", _TASK_SOURCE)):
        if not (pkg / fname).exists():
            (pkg / fname).write_text(template.format(name=name, cls=cls))

    if into is not None:
        seed_kit(incoming.seed, Path(into))
    return pkg


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI shim
    import argparse

    p = argparse.ArgumentParser(prog="orchestrator-scaffold",
                                description="Generate a project-config adapter from a profile.")
    p.add_argument("--name", help="adapter/project name (defaults to the --detect repo's dir name)")
    p.add_argument("--dest", default=None,
                   help="destination dir for the package (default: <into>/.orchestration when "
                        "--into is given — the adapter lives with the project; else adapters/project)")
    p.add_argument("--profile", help="path to a profile.toml (the interview writes this)")
    p.add_argument("--languages", help="comma-separated stack to synthesize a profile from (e.g. python,typescript)")
    p.add_argument("--into", help="target project root to seed .claude/ assets into")
    p.add_argument("--detect", help="detect a repo's stack and PRINT a draft profile.toml (no files written)")
    args = p.parse_args(argv)

    manifest = load_kit_manifest()

    # --detect is the interview's first step: print a draft profile for the human to confirm.
    if args.detect:
        name = args.name or Path(args.detect).resolve().name
        print(profile_to_toml(detect_profile(args.detect, name, manifest)), end="")
        return 0

    if not args.name:
        p.error("--name is required (unless using --detect)")
    if args.profile:
        prof: Profile | None = read_profile(Path(args.profile))
    elif args.languages:
        prof = profile_from_languages(args.name, args.languages.split(","), manifest)
    else:
        prof = None

    if args.dest is None and args.into:
        # Project-owned layout: the adapter lives in the project's repo, loaded by path.
        path = scaffold_adapter(args.name, profile=prof, into=args.into,
                                package_dir=Path(args.into) / ".orchestration")
    else:
        path = scaffold_adapter(args.name, args.dest or "adapters/project",
                                profile=prof, into=args.into)
    print(f"scaffolded adapter: {path}")
    if args.into:
        print(f"seeded starter kit into: {Path(args.into) / '.claude'}")
    print("Next: review profile.toml + fill classifier.py / task_source.py for this project.")
    print(f"Check it: uv run orchestrator --project {path} validate")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
