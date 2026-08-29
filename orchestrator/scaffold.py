"""Project bootstrap — profile-driven adapter generation + starter-kit seeding (§5).

Standing up a new project for orchestration means writing ONE project-config adapter
(the engine is never touched). This module generates that adapter from a **profile**
(the project's stack + commands + roster + layers) and seeds the stack-appropriate
subset of the starter kit (``templates/project-default/``) into the new project's
``.claude/``. It is deterministic ("profile in -> files out"), idempotent, and
*additive* on re-run (it extends a project without clobbering hand-edits).

A generated adapter also carries the worktree-provenance defenses this repo wrote for
itself (#391): a copied virtualenv keeps the ORIGINATING worktree's interpreter in its
console-script shebangs, so without them a REVIEW stage can test a different worktree's
source and approve on a false green. They are derived from the profile — the probe follows
the form the project actually declares, proving a console script's shebang or (for the
``python -m ...`` default, #396) the interpreter the runner resolves to — and live in
profile.toml's ``[worktree]`` table, so a project can tune them without hand-editing
generated code.

The interactive interview that *composes* a profile (detect stack -> ask -> tune) is a
separate run-target skill; this is the deterministic layer it drives.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.ports.project import ADAPTER_CONTRACT_VERSION


def _kit_dir() -> Path:
    """Locate the starter kit in both a source checkout and an installed wheel.

    In an installed wheel the kit is force-included under the package
    (``orchestrator/templates/project-default``); in a source checkout it lives at the
    repo root (``../templates/project-default``). Prefer whichever exists so the same code
    path works CWD-independently in either layout.
    """
    here = Path(__file__).resolve().parent
    packaged = here / "templates" / "project-default"
    if packaged.is_dir():
        return packaged
    return here.parent / "templates" / "project-default"


KIT_DIR = _kit_dir()

# Command method <-> profile key. Order is the order they appear in the generated config.
#
# The two static-analysis rows are NOT a straight name match, and #412 is why. A profile
# names its commands by TOOL ROLE (``lint`` = ruff, ``typecheck`` = mypy), but the engine
# names its two legs ``typecheck_cmd`` (the LINT leg) and ``types_cmd`` (the STATIC-TYPING
# leg, #243) — see ``Engine._run_verification_commands``. Mapping ``typecheck`` ->
# ``typecheck_cmd`` reads correct and is not: it puts the type checker in the lint slot and
# leaves ``types_cmd`` absent, so both merge gates ran mypy under the label "typecheck" and
# never ran the linter at all. Map by LEG, not by name.
_COMMAND_METHODS: list[tuple[str, str, bool]] = [
    # (method_name, primary_profile_key, takes_files_arg)
    ("install_cmd", "install", False),
    ("test_unit_cmd", "test_unit", True),
    ("test_e2e_cmd", "test_e2e", True),
    ("test_shell_cmd", "test_shell", True),
    ("typecheck_cmd", "lint", False),
    ("types_cmd", "typecheck", False),
    ("infra_reset", "infra_reset", False),
]

# Comments rendered above a generated method, so the leg/name mismatch above is explained
# in the adapter a project actually reads — mirroring adapters/project/selfhost/config.py.
_METHOD_NOTES: dict[str, str] = {
    "typecheck_cmd": (
        "    # The engine's LINT leg (#412): profile.toml's `lint` command. The METHOD name is\n"
        "    # the engine's, the TOOL is the linter. Run by the REVIEW gate below and by both\n"
        "    # merge gates (batch integration + trunk).\n"
    ),
    "types_cmd": (
        "    # The engine's STATIC-TYPING leg (#243): profile.toml's `typecheck` command, kept\n"
        "    # distinct from the linter above so a merge gate runs BOTH. A project with only one\n"
        "    # static-analysis tool puts it on typecheck_cmd and leaves this a no-op.\n"
    ),
}

# The generic roster used when no stack-specific agents are selected (no-profile default).
_DEFAULT_ROSTER: dict[str, str] = {
    "implement": "generic-implementer",
    "simplify": "code-simplifier",
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
    # Worktree-provenance declarations (#391): which dependency artifacts a disposable
    # REVIEW checkout must rebuild, and which launchers/modules must resolve inside the
    # worktree under test. Derived from the stack + commands; hand-editable in profile.toml.
    worktree: dict[str, list[str]] = field(default_factory=dict)
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
    # Hooks mirror agents: untagged = always (the safety guards must reach every
    # project, not just tagged stacks); tagged = per-language.
    hooks = [
        h for h, spec in manifest["hooks"].items()
        if not spec.get("tags") or langs.intersection(spec["tags"])
    ]
    skills = list(manifest.get("skills", {}).get("always", []))
    return {"agents": sorted(agents), "hooks": sorted(hooks), "skills": skills}


# Runner prefixes a project's commands may carry, mapped to the interpreter invocation a
# worktree-origin probe must use. Only the FIRST match against the declared commands wins.
_PYTHON_RUNNERS: list[tuple[list[str], list[str]]] = [
    (["uv", "run"], ["uv", "run", "python"]),
    (["poetry", "run"], ["poetry", "run", "python"]),
    (["pipenv", "run"], ["pipenv", "run", "python"]),
    (["python", "-m"], ["python"]),
]

# Package managers whose console scripts live in the project's OWN ``.venv/bin``. poetry and
# pipenv keep the environment outside the tree by default, so a ``.venv/bin/<script>`` probe
# would fail on a perfectly healthy worktree — those profiles get the module probe only.
_IN_TREE_VENV_RUNNERS: set[tuple[str, ...]] = {("uv", "run")}

# The gates whose green/red decides a stage's verdict — so these are the launchers whose
# origin must be proven. Probing every declared command would only add subprocess cost.
_PROBED_COMMAND_KEYS = ("test_unit", "typecheck")


def _python_runner(commands: dict[str, list[str]]) -> tuple[list[str], list[str]] | None:
    """The (prefix, python-invocation) pair this profile's python commands run through."""
    for key in (*_PROBED_COMMAND_KEYS, "lint", "install"):
        argv = commands.get(key) or []
        for prefix, runner in _PYTHON_RUNNERS:
            if argv[: len(prefix)] == prefix:
                return prefix, runner
    return None


def derive_worktree(languages: list[str], commands: dict[str, list[str]], manifest: dict) -> dict[str, list[str]]:
    """Default worktree-provenance declarations for a stack (#391).

    A copied virtualenv carries console-script shebangs hardcoded to the ORIGINATING
    worktree's interpreter, so a review can run its tests against another worktree's source
    and approve on a false green. ``fresh_install_paths`` makes the disposable REVIEW
    checkout rebuild instead of copying; the probes make a wrong origin LOUD rather than
    merely unlikely. Everything here is derived from what the profile actually declares, so
    the probe proves the thing the project really runs.

    WHICH probe that is follows the command form (#396). A bare console script
    (``uv run pytest``) is proven by its shebang, so it yields a ``launcher_probes`` entry.
    Module invocation (``uv run python -m pytest``, the kit default) has no shebang to go
    stale — the runner resolves the interpreter directly — so the thing worth proving is the
    INTERPRETER, and it yields ``interpreter_probe`` instead. Without that, moving the
    defaults to ``python -m`` would have quietly left a python profile with no probe at all:
    the hazard would be unlikely, but no longer loud.

    A mixed-language profile can have ``_PROBED_COMMAND_KEYS`` claimed by a NON-python
    toolchain (e.g. typescript's ``typecheck`` is ``pnpm exec tsc``); such a key is skipped
    rather than sliced at the python runner's offset, which would otherwise derive a
    launcher that can never exist in the venv (see the ``argv[:len(prefix)]`` guard below).
    """
    worktree: dict[str, list[str]] = {}
    for lang in languages:
        defaults = manifest.get("worktree", {}).get(lang, {})
        if not defaults:
            continue
        for path in defaults.get("fresh_install_paths", []):
            worktree.setdefault("fresh_install_paths", [])
            if path not in worktree["fresh_install_paths"]:
                worktree["fresh_install_paths"].append(path)
        launcher_dir = defaults.get("launcher_dir")
        resolved = _python_runner(commands) if lang == "python" else None
        if not launcher_dir or resolved is None:
            continue
        prefix, runner = resolved
        worktree.setdefault("python", list(runner))
        if tuple(prefix) not in _IN_TREE_VENV_RUNNERS:
            continue
        for key in _PROBED_COMMAND_KEYS:
            argv = commands.get(key) or []
            if argv[: len(prefix)] != prefix:
                # A mixed-language profile can have this gate claimed by ANOTHER toolchain
                # (typescript's typecheck is `pnpm exec tsc`). Slicing it at the python
                # runner's offset would derive a launcher that can never exist in the venv,
                # and the probe would silently pass forever. Skip rather than mis-slice.
                continue
            launcher = argv[len(prefix)] if len(argv) > len(prefix) else ""
            if not launcher:
                continue
            if launcher in ("python", "python3"):
                # Module invocation: no shebang to go stale, so there is no launcher worth
                # probing — but the INTERPRETER this runner resolves to still has to come
                # from the worktree under test (a stray VIRTUAL_ENV or a project-environment
                # override points it elsewhere). Prove that instead, once (#396).
                worktree.setdefault("interpreter_probe", list(runner))
                continue
            path = f"{launcher_dir}/{launcher}"
            worktree.setdefault("launcher_probes", [])
            if path not in worktree["launcher_probes"]:
                worktree["launcher_probes"].append(path)
    return worktree


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
    profile.worktree = derive_worktree(langs, commands, manifest)
    profile.seed = select_kit_assets(profile, manifest)
    return profile


def _overrides(
    profile: Profile, manifest: dict
) -> tuple[dict[str, list[str]], dict[str, str], dict[str, list[str]]]:
    """A profile's HAND-edits: the commands/roster/worktree entries that differ from what
    profile_from_languages would derive as defaults for its own languages. This lets a
    re-run keep human overrides without a fully-defaulted incoming profile clobbering them."""
    base = profile_from_languages(profile.name, profile.languages, manifest)
    cmd = {k: v for k, v in profile.commands.items() if base.commands.get(k) != v}
    roster = {k: v for k, v in profile.roster.items() if base.roster.get(k) != v}
    worktree = {k: v for k, v in profile.worktree.items() if base.worktree.get(k) != v}
    return cmd, roster, worktree


def merge_profiles(existing: Profile, incoming: Profile, manifest: dict) -> Profile:
    """Additive merge for a re-run: union the languages, re-derive defaults for the union,
    then re-apply hand-overrides (incoming's win over existing's). Nothing is removed."""
    languages = list(dict.fromkeys([*existing.languages, *incoming.languages]))
    merged = profile_from_languages(existing.name, languages, manifest)
    merged.task_source = incoming.task_source or existing.task_source
    ex_cmd, ex_roster, ex_worktree = _overrides(existing, manifest)
    in_cmd, in_roster, in_worktree = _overrides(incoming, manifest)
    merged.commands.update({**ex_cmd, **in_cmd})
    merged.roster.update({**ex_roster, **in_roster})
    # Re-derive from the merged commands first, so a newly added language's defaults land,
    # then re-apply hand edits (and detection results, which read as edits) on top.
    merged.worktree = derive_worktree(merged.languages, merged.commands, manifest)
    merged.worktree.update({**ex_worktree, **in_worktree})
    merged.layers = {**existing.layers, **incoming.layers}
    merged.seed = select_kit_assets(merged, manifest)
    return merged


# ---------------------------------------------------------------------------------------
# Stack detection (the deterministic first-guess the interview skill presents)
# ---------------------------------------------------------------------------------------

# Package-manager command sets, keyed by lockfile-detected PM. These OVERRIDE the
# manifest's per-language defaults (a lockfile is stronger evidence than the default PM).
_PM_COMMANDS: dict[str, dict[str, list[str]]] = {
    # Module invocation for the same reason as the manifest default (#396): a console
    # script's shebang can name another worktree's interpreter, `python -m` cannot.
    "poetry": {"install": ["poetry", "install"],
               "test_unit": ["poetry", "run", "python", "-m", "pytest", "-q"],
               "lint": ["poetry", "run", "python", "-m", "ruff", "check", "."],
               "typecheck": ["poetry", "run", "python", "-m", "mypy", "."]},
    "pip": {"install": ["pip", "install", "-r", "requirements.txt"],
            "test_unit": ["python", "-m", "pytest", "-q"],
            "lint": ["python", "-m", "ruff", "check", "."]},
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


# Directories that look like a package but are not the project's own importable source.
_NON_PACKAGE_DIRS = frozenset({
    "tests", "test", "testing", "docs", "doc", "scripts", "examples", "example",
    "build", "dist", "site-packages", "node_modules", "venv", "migrations",
})


def _detect_package(root: Path) -> str | None:
    """The project's own importable top-level package, for a 'source' origin probe.

    A launcher probe proves the RUNNER came from this worktree; this proves the imported
    project code did too. Returns None when no unambiguous package exists (a flat script
    repo, or src-less namespace layout) — the scaffold then declares no module probe and
    says so in the generated adapter rather than guessing a name that would not import.
    """
    if not root.is_dir():
        return None
    src = root / "src"
    roots = [src] if src.is_dir() else [root]
    for parent in roots:
        for path in sorted(parent.iterdir()):
            if not path.is_dir() or path.name.startswith((".", "_")):
                continue
            if path.name in _NON_PACKAGE_DIRS or not (path / "__init__.py").exists():
                continue
            return path.name
    return None


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
    # Re-derive from the DETECTED commands (the package manager may differ from the
    # manifest default), then add the module probe only if a real package was found.
    profile.worktree = derive_worktree(languages, profile.commands, manifest)
    if profile.worktree.get("python") and (pkg := _detect_package(root)):
        profile.worktree["source_modules"] = [pkg]
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
        ("layers", profile.layers), ("worktree", profile.worktree), ("seed", profile.seed),
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
        worktree={k: list(v) for k, v in raw.get("worktree", {}).items()},
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
GitHub-Issues source (see ``adapters/project/selfhost/task_source.py`` for the shape).
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.ports.project import TaskSpec
from orchestrator.errors import OrchestratorError


class LocalTaskSource:
    """Tasks from a JSON file: {{"<id>": {{"title", "body", "depends_on", "labels"}}}}.

    ``labels`` is read by engine policies (e.g. the meta-authoring delivery gate), so
    omitting it silently opts a task out of them."""

    def __init__(self, tasks_path: str | Path) -> None:
        self.tasks_path = Path(tasks_path)

    def resolve(self, task_id: str) -> TaskSpec:
        """Resolve a local task snapshot, including labels used by engine policies."""
        if not self.tasks_path.exists():
            raise OrchestratorError(f"tasks file not found: {{self.tasks_path}}")
        data = json.loads(self.tasks_path.read_text())
        if task_id not in data:
            raise OrchestratorError(f"unknown task {{task_id!r}}")
        t = data[task_id]
        return TaskSpec(task_id=task_id, title=t.get("title", ""), body=t.get("body", ""),
                        depends_on=list(t.get("depends_on", [])),
                        labels=list(t.get("labels", [])))

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None:
        with open(self.tasks_path.with_name("completed.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{{task_id}}\\t{{pr_url or ''}}\\n")
'''


def _resolve_command(method: str, key: str, commands: dict[str, list[str]]) -> list[str] | None:
    """The argv a generated method returns, after mapping profile keys onto engine legs.

    Straight ``commands[key]`` for everything except the two static-analysis legs (#412),
    which are mapped by LEG rather than by name (see ``_COMMAND_METHODS``): the linter goes
    to ``typecheck_cmd`` and the type checker to ``types_cmd``.

    A profile declaring only ONE static-analysis command puts it on the primary leg
    (``typecheck_cmd``) and leaves ``types_cmd`` a no-op — the shape
    ``orchestrator/ports/project.py`` documents for a TS project whose ``typecheck_cmd``
    IS ``tsc --noEmit``. Without that fallback such a project would leave its only gate on
    the duck-typed optional leg.
    """
    if method == "typecheck_cmd":
        return commands.get("lint") or commands.get("typecheck")
    if method == "types_cmd":
        return commands.get("typecheck") if commands.get("lint") else None
    return commands.get(key)


def _command_method(method: str, key: str, takes_files: bool, profile: Profile, name: str) -> str:
    sig_arg = "self, files: list[str] | None = None" if takes_files else "self"
    argv = _resolve_command(method, key, profile.commands)
    note = _METHOD_NOTES.get(method, "")
    if argv:
        body = f"        return {_argv_literal(argv)}"
    elif key == "test_unit":
        # FAIL-CLOSED until set, so an unconfigured run can't vacuously pass.
        body = (f"        return [\"sh\", \"-c\", "
                f"\"echo 'orchestrator: set {name} test_unit_cmd' >&2; exit 1\"]")
    else:
        body = "        return _NOOP"
    return f"{note}    def {method}({sig_arg}) -> list[str]:\n{body}"


_WORKTREE_HELPERS = """

# Worktree-origin probes (#391, #396) — generated from profile.toml [worktree]. A
# virtualenv copied from another worktree keeps THAT worktree's interpreter in its
# console-script shebangs, so `{runner} pytest` can validate the wrong source and a review
# can approve on a false green. Commands declared in the `{runner} python -m ...` form dodge
# the shebang entirely, but the interpreter the runner picks can still come from outside
# (a stray VIRTUAL_ENV, a redirected project environment) — hence the interpreter probe.
# Each probe prints the path a launcher, interpreter, or import really resolves to; the
# execution adapter refuses any path that lands outside the worktree under test.
_PROBE_PY = {runner_argv}


def _launcher_probe(relative: str) -> tuple[str, list[str], str]:
    \"\"\"Read a console script's shebang — a stale one names another worktree's python.

    A launcher the project never installed is NOT the hazard this guards: a missing file
    cannot point at another worktree. Absence therefore falls back to the interpreter
    running the probe — still inside this worktree if the environment is honest, and still
    caught if it is not — rather than failing a worktree for a tool it does not have.
    \"\"\"
    code = ("import sys; from pathlib import Path; "
            f"p = Path({{relative!r}}); "
            "print(p.read_text().splitlines()[0].removeprefix('#!') if p.is_file() "
            "else sys.executable)")
    return (f"{{relative}} shebang interpreter", [*_PROBE_PY, "-c", code], "launcher")


def _module_probe(module: str) -> tuple[str, list[str], str]:
    \"\"\"Resolve an imported project module to the file it was actually loaded from.\"\"\"
    code = f"import {{module}} as _m; print(_m.__file__)"
    return (f"{{module}} module", [*_PROBE_PY, "-c", code], "source")


def _interpreter_probe(argv: list[str]) -> tuple[str, list[str], str]:
    \"\"\"Resolve the interpreter a `python -m ...` command actually runs on.

    Module invocation has no shebang to inherit, so this is what a wrong environment looks
    like instead: the runner resolving to a venv outside the worktree under test. Reported
    as a launcher, because a healthy `.venv/bin/python` is itself a symlink to a SHARED base
    interpreter — only its parent directory has to live inside the worktree.
    \"\"\"
    label = " ".join(argv)
    code = "import sys; print(sys.executable)"
    return (f"{{label}} interpreter", [*argv, "-c", code], "launcher")
"""

_NO_PROBES_COMMENT = """
    # No worktree-origin probes are declared: this stack has no derivable launcher,
    # interpreter, or importable module to prove came from the worktree under test.
    # Verification is therefore SKIPPED (observably — the engine emits a
    # skipped-verification notice). Add `launcher_probes` / `interpreter_probe` /
    # `source_modules` under [worktree] in profile.toml to close it.
"""


def _worktree_methods(profile: Profile) -> tuple[str, str]:
    """Render (module-level helpers, class methods) for the worktree-provenance hooks.

    A profile that declares nothing generates nothing, so a stack without these defaults is
    byte-identical to a pre-#391 scaffold.
    """
    wt = profile.worktree
    fresh, launchers = wt.get("fresh_install_paths", []), wt.get("launcher_probes", [])
    modules, runner = wt.get("source_modules", []), wt.get("python", [])
    interpreter = wt.get("interpreter_probe", [])
    probed = launchers or interpreter or modules
    if not (fresh or (probed and runner)):
        return "", ""

    methods = ""
    if fresh:
        methods += f"""
    def fresh_install_paths(self) -> list[str]:
        \"\"\"Dependency artifacts a disposable REVIEW checkout must rebuild, never copy.\"\"\"
        return {_argv_literal(fresh)}
"""
    if not runner or not probed:
        return "", methods + _NO_PROBES_COMMENT

    sources = []
    if launchers:
        sources.append(f"[_launcher_probe(p) for p in {_argv_literal(launchers)}]")
    if interpreter:
        sources.append(f"[_interpreter_probe({_argv_literal(interpreter)})]")
    if modules:
        sources.append(f"[_module_probe(m) for m in {_argv_literal(modules)}]")
    body = "\n            + ".join(sources)
    methods += f"""
    def worktree_origin_probes(self) -> list[tuple[str, list[str], str]]:
        \"\"\"Prove the runner and the imported source both come from THIS worktree.\"\"\"
        return (
            {body}
        )
"""
    helpers = _WORKTREE_HELPERS.format(
        runner=" ".join(runner[:-1]) or "python", runner_argv=_argv_literal(runner)
    )
    return helpers, methods


def render_config(name: str, profile: Profile) -> str:
    """Render config.py as a generated VIEW of the profile (commands + roster baked in)."""
    cls, env = _class_name(name), _env_var(name)
    roster_lines = "".join(f"    {k!r}: {v!r},\n" for k, v in (profile.roster or _DEFAULT_ROSTER).items())
    commands = "\n\n".join(
        _command_method(m, k, f, profile, name) for m, k, f in _COMMAND_METHODS
    )
    worktree_helpers, worktree_methods = _worktree_methods(profile)
    return f'''"""{name} project-config adapter (GENERATED from profile.toml — do not hand-edit).

Edit profile.toml (or re-run orchestrator-scaffold) and this file is regenerated.
The engine is never edited — only this adapter. classifier.py and task_source.py are
written once and are yours to tune.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from adapters.project.email_sink import email_sink_from_env
from orchestrator.schemas.enums import Stage
from orchestrator.schemas.stage_schemas import resolve_stage_schema

from .classifier import {cls}Classifier
from .task_source import LocalTaskSource

_NOOP = ["true"]

# Review-gate bounds mirror the self-host adapter: keep the useful output tail without
# allowing a noisy tool to consume the review context, and never wait forever on a gate.
_GATE_TIMEOUT_S = 300
_GATE_OUTPUT_CAP = 2000

# Optional per-stage schema overrides live next to this file (CWD-independent); absent
# a <ref>.json the engine's canonical stage contract is used (codex full-validation).
_SCHEMA_DIR = Path(__file__).parent / "schemas"

# Stage (sub-)role -> agent name (from the starter kit), baked from profile.toml [roster].
_ROSTER: dict[str, str] = {{
{roster_lines}}}
{worktree_helpers}

class {cls}:
    name = "{name}"

    # The deterministic INTAKE runner creates task worktrees from this path; without it,
    # intake falls back to the DRIVER's process CWD — the engine checkout, i.e. the wrong
    # repo (#368). The adapter always lives at <repo>/.orchestration, so self-locate.
    repo_root = str(Path(__file__).resolve().parent.parent)

    def __init__(self, tasks_path: str = "tasks.json") -> None:
        self._classifier = {cls}Classifier()
        self._task_source = LocalTaskSource(tasks_path)

{commands}
{worktree_methods}
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

    def review_findings(self, *, worktree: str | None = None) -> list[dict]:
        """Run profile-declared lint/typecheck policy gates in the task worktree.

        A red command blocks REVIEW. A command that cannot run is advisory instead: the
        gate is visibly UNVERIFIED without deadlocking a task on a missing tool or timeout.

        The methods are the ENGINE's leg names, not the tools' (#412): ``typecheck_cmd`` is
        the lint leg, ``types_cmd`` the static-typing leg. Calling exactly the pair both
        merge gates call is what keeps REVIEW and merge from checking different things.
        """
        if not worktree or not os.path.isdir(worktree):
            return []
        findings: list[dict] = []
        for label, argv in (("lint", self.typecheck_cmd()),
                            ("typecheck", self.types_cmd())):
            if (finding := self._gate(worktree, label, argv)) is not None:
                findings.append(finding)
        return findings

    def _gate(self, worktree: str, label: str, argv: list[str]) -> dict | None:
        """Run one configured gate leg and translate its outcome into a finding."""
        if not argv or argv == _NOOP:
            return None
        command = " ".join(argv)
        try:
            proc = self._run_gate(argv, worktree)
        except Exception as exc:  # noqa: BLE001 - a policy hook must never break record()
            return {{
                "description": f"{{label}} gate could not run in the task worktree "
                               f"({{type(exc).__name__}}: {{exc}}). The gate is UNVERIFIED "
                               "for this change.",
                "severity": "important",
                "blocking": False,
            }}
        if proc.returncode == 0:
            return None
        detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if len(detail) > _GATE_OUTPUT_CAP:
            detail = "…\\n" + detail[-_GATE_OUTPUT_CAP:]
        return {{
            "description": f"{{label}} gate is RED on this change — profile.toml declares "
                           f"`{{command}}` as a project gate. Output:\\n{{detail}}",
            "severity": "critical",
            "suggested_fix": f"Run `{{command}}` in the worktree and fix what it reports.",
            "blocking": True,
        }}

    @staticmethod
    def _run_gate(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        """Injectable subprocess seam for deterministic review-gate tests."""
        return subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=_GATE_TIMEOUT_S, check=False
        )

    def notify(self, kind: str, payload: dict) -> None:
        """Alerting sink: always a stderr line; additionally mails the payload when the
        environment configures SMTP (see ``adapters.project.email_sink`` for the
        ``ORCHESTRATOR_SMTP_*`` / ``ORCHESTRATOR_NOTIFY_EMAIL_TO`` vars). Unconfigured,
        the email half is a silent no-op — so this is safe to ship on by default, and a
        batch is never silently silent: without it, a detached driver's completion is
        undiscoverable except by polling ``status``. Swallows all errors; an alert sink
        must never break a run."""
        print(f"[orchestrator:{{kind}}] {{payload.get('summary') or kind}}", file=sys.stderr)
        try:
            if sink := email_sink_from_env():
                sink(kind, payload)  # swallows its own errors; short socket timeout
        except Exception:  # noqa: BLE001 - an alert sink must never break the run
            pass

    # --- optional seams the engine duck-types (add methods here to opt in) -----------
    #
    # review_findings above wires profile.toml's lint/typecheck commands by default. Extend
    # it here for project-specific policies (for example, "frontend changes need e2e specs").
    #
    # def publish_progress(self, ...) / publish_note(self, ...) /
    # file_followup(self, ...) / file_followup_keyed(self, ..., idempotency_key=...):
    #     Task-source write-backs: living progress comment, completion note, follow-up
    #     issue filing. See orchestrator/ports/project.py for the full optional surface.


def get_config() -> {cls}:
    return {cls}(tasks_path=os.environ.get("{env}", "tasks.json"))
'''


# ---------------------------------------------------------------------------------------
# Seeding + orchestration
# ---------------------------------------------------------------------------------------

def _skill_slug(src: Path) -> str:
    """The invocable skill name for a kit skill file = its frontmatter ``name:``.

    Claude Code discovers skills at ``.claude/skills/<name>/SKILL.md`` (a directory per
    skill), so the seed destination is keyed by the frontmatter name, not the source file
    stem. Falls back to the file stem if the frontmatter has no ``name:``."""
    in_frontmatter = False
    for line in src.read_text().splitlines():
        stripped = line.strip()
        if stripped == "---":
            if in_frontmatter:
                break  # end of the frontmatter block; no name found
            in_frontmatter = True
            continue
        if in_frontmatter and stripped.startswith("name:"):
            return stripped.split(":", 1)[1].strip()
    return src.stem


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
        # Seed as .claude/skills/<name>/SKILL.md so the project gets a real, invocable
        # `/<name>` slash command (not a flat, undiscovered .md file).
        src = KIT_DIR / "skills" / f"{skill}.md"
        dst = claude / "skills" / _skill_slug(src) / "SKILL.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        seeded.append(f"skills/{dst.parent.name}/SKILL.md")
    hook_files: list[Path] = []
    for hook in assets.get("hooks", []):
        dst = claude / "hooks" / f"{hook}.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(KIT_DIR / "hooks" / f"{hook}.json", dst)
        seeded.append(f"hooks/{hook}.json")
        hook_files.append(dst)
    # A hook fragment on disk is inert — it only fires once it lives in the project's
    # .claude/settings.json. Merge the seeded fragments in (idempotent, additive), so
    # the safety guards and format hooks are LIVE from the first scaffold, not an
    # example the user has to hand-wire.
    if hook_files and _merge_hook_settings(claude, hook_files):
        seeded.append("settings.json (hooks merged)")
    return seeded


def _merge_hook_settings(claude: Path, hook_files: list[Path]) -> bool:
    """Merge hook fragments ({"hooks": {event: [entries]}}) into ``.claude/settings.json``.
    Additive and idempotent: an entry already present (exact match) is never duplicated;
    other settings keys are preserved. Returns True when the file changed."""
    settings_path = claude / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except ValueError:
            return False  # never clobber a hand-edited-but-broken settings file
    hooks = settings.setdefault("hooks", {})
    changed = not settings_path.exists()
    for hf in hook_files:
        try:
            fragment = json.loads(hf.read_text(encoding="utf-8")).get("hooks", {})
        except ValueError:
            continue
        for event, entries in fragment.items():
            bucket = hooks.setdefault(event, [])
            for entry in entries:
                if entry not in bucket:
                    bucket.append(entry)
                    changed = True
    if changed:
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return changed


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
    if package_dir is not None:
        pkg = Path(package_dir)
    elif dest_dir is not None:
        pkg = Path(dest_dir) / name.replace("-", "_")
    else:
        raise ValueError("scaffold_adapter needs dest_dir or package_dir")
    manifest = load_kit_manifest()
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
