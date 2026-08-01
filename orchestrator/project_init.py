"""Phase-0 project skeleton generation (#367) — the ground the harness stands on.

``scaffold.py`` generates a project's *adapter* from a profile, but it only ever **detects**
an existing stack (``_detect_languages`` reads ``pyproject.toml`` / ``package.json`` / …).
On an empty folder it finds nothing, so standing up a NEW project began with an unautomated
half hour of boilerplate — and getting it wrong (an unconfigured type-checker, no passing
test) leaves the harness's quality gates silently passing forever.

This module is the missing half: name + path + stack in, a working repo out. It is
deterministic ("skeleton in -> files out"), calls no model, and knows nothing about any
specific project. The conversation that *composes* the inputs — and the oversight of the
phase-1 hand-off — lives in the ``new-project`` skill, exactly as ``spec_intake`` pairs
with the ``spec-intake`` skill.

Two design points worth keeping:

* **It verifies its own output.** Writing a `pyproject.toml` that *configures* mypy is not
  the same as a repo where ``uv run mypy`` exits 0. The skeleton's declared ``verify``
  commands are run before this reports success, because those exact commands become the
  adapter's verification commands in phase 1 — a skeleton that cannot pass them has failed
  at the only thing it exists to do.
* **The GitHub repo is created last, and only on green.** Repo creation is outward-facing
  and awkward to undo; it happens after the local skeleton is proven, never before, and
  never unless explicitly asked for.
"""

from __future__ import annotations

import keyword
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .errors import OrchestratorError


class ProjectInitError(OrchestratorError):
    """A project skeleton could not be planned or written."""


def _skeleton_root() -> Path:
    """Locate the skeleton templates in both a source checkout and an installed wheel.

    Mirrors ``scaffold._kit_dir``: the wheel force-includes ``templates`` under the package
    (``orchestrator/templates/project-skeleton``); a source checkout keeps it at the repo
    root. Prefer whichever exists so the same code path works CWD-independently.
    """
    here = Path(__file__).resolve().parent
    packaged = here / "templates" / "project-skeleton"
    if packaged.is_dir():
        return packaged
    return here.parent / "templates" / "project-skeleton"


SKELETON_ROOT = _skeleton_root()

# Only Python to start (#367). A second stack doubles the template surface and the test
# matrix, so it waits for a real project that needs it rather than being speculated at.
DEFAULT_STACK = "python"

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


@dataclass(frozen=True)
class Skeleton:
    """A parsed ``templates/project-skeleton/<stack>/manifest.toml``."""

    stack: str
    directory: Path
    description: str
    verify: list[list[str]]
    files: dict[str, str]


@dataclass(frozen=True)
class InitPlan:
    """Everything ``init_project`` would write, resolved but not yet on disk."""

    name: str
    package: str
    root: Path
    stack: str
    description: str
    files: dict[str, str] = field(default_factory=dict)
    verify: list[list[str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "package": self.package,
            "root": str(self.root),
            "stack": self.stack,
            "description": self.description,
            "files": sorted(self.files),
            "verify": [" ".join(cmd) for cmd in self.verify],
        }


@dataclass(frozen=True)
class CommandResult:
    """One shelled command's outcome (the injectable seam tests replace)."""

    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def as_dict(self) -> dict[str, object]:
        out = {"cmd": " ".join(self.argv), "returncode": self.returncode, "ok": self.ok}
        if not self.ok:
            # Only carry output on failure — a green run's chatter is noise in the report.
            out["stderr"] = self.stderr.strip()[-2000:]
            out["stdout"] = self.stdout.strip()[-2000:]
        return out


CommandRunner = Callable[[Sequence[str], Path], CommandResult]


def run_command(argv: Sequence[str], cwd: Path) -> CommandResult:
    """Default runner: shell ``argv`` in ``cwd`` and capture its output."""
    try:
        proc = subprocess.run(  # noqa: S603 - argv is template/manifest-declared, never user prose
            list(argv), cwd=str(cwd), capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as exc:
        return CommandResult(list(argv), 127, "", f"command not found: {exc}")
    return CommandResult(list(argv), proc.returncode, proc.stdout, proc.stderr)


# ---------------------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------------------

def normalize_name(raw: str) -> str:
    """Normalize a project name to the kebab-case form used for the dir, repo, and dist.

    Accepts what a human types ("Prediction Markets", "prediction_markets") and refuses
    what cannot be a package or repo name, rather than silently mangling it.
    """
    name = re.sub(r"[\s_]+", "-", raw.strip().lower())
    name = re.sub(r"-{2,}", "-", name).strip("-")
    if not name:
        raise ProjectInitError(f"project name {raw!r} normalizes to empty")
    if not _NAME_RE.match(name):
        raise ProjectInitError(
            f"project name {raw!r} -> {name!r} is not usable: names must be lowercase "
            "alphanumerics separated by single hyphens (a-z, 0-9, -)"
        )
    return name


def package_name(name: str) -> str:
    """The import package for a normalized project name (``a-b`` -> ``a_b``)."""
    package = name.replace("-", "_")
    if not package.isidentifier() or keyword.iskeyword(package):
        raise ProjectInitError(
            f"project name {name!r} yields import package {package!r}, which is not a "
            "valid Python identifier — pick a different name"
        )
    return package


# ---------------------------------------------------------------------------------------
# Skeleton + plan
# ---------------------------------------------------------------------------------------

def available_stacks() -> list[str]:
    """The stacks with a skeleton template, for a useful error on an unknown one."""
    if not SKELETON_ROOT.is_dir():
        return []
    return sorted(d.name for d in SKELETON_ROOT.iterdir() if (d / "manifest.toml").is_file())


def load_skeleton(stack: str = DEFAULT_STACK) -> Skeleton:
    """Parse a stack's skeleton manifest."""
    directory = SKELETON_ROOT / stack
    manifest_path = directory / "manifest.toml"
    if not manifest_path.is_file():
        known = ", ".join(available_stacks()) or "(none found)"
        raise ProjectInitError(f"no skeleton for stack {stack!r}; available: {known}")
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    meta = manifest.get("skeleton", {})
    files = manifest.get("files", {})
    if not files:
        raise ProjectInitError(f"skeleton {stack!r} declares no [files]")
    return Skeleton(
        stack=stack,
        directory=directory,
        description=str(meta.get("description", "")),
        verify=[list(cmd) for cmd in meta.get("verify", [])],
        files={str(k): str(v) for k, v in files.items()},
    )


def _substitute(text: str, *, name: str, package: str, description: str) -> str:
    return (
        text.replace("{{PACKAGE}}", package)
        .replace("{{NAME}}", name)
        .replace("{{DESCRIPTION}}", description)
    )


def plan_project(
    raw_name: str,
    parent: Path,
    *,
    stack: str = DEFAULT_STACK,
    description: str = "",
    package: str | None = None,
) -> InitPlan:
    """Resolve a full skeleton plan without touching the filesystem.

    ``parent`` is the directory the project dir is created *inside* (e.g. ``~/Development``),
    so the project lands at ``<parent>/<name>``.
    """
    name = normalize_name(raw_name)
    pkg = package or package_name(name)
    if not pkg.isidentifier() or keyword.iskeyword(pkg):
        raise ProjectInitError(f"package name {pkg!r} is not a valid Python identifier")
    skeleton = load_skeleton(stack)
    desc = description.strip() or f"{name} — a new project."

    files: dict[str, str] = {}
    for template, dest in skeleton.files.items():
        source = skeleton.directory / template
        if not source.is_file():
            raise ProjectInitError(f"skeleton {stack!r} names a missing template: {template}")
        rendered_dest = _substitute(dest, name=name, package=pkg, description=desc)
        files[rendered_dest] = _substitute(
            source.read_text(encoding="utf-8"), name=name, package=pkg, description=desc
        )
    return InitPlan(
        name=name,
        package=pkg,
        root=(parent.expanduser().resolve() / name),
        stack=stack,
        description=desc,
        files=files,
        verify=skeleton.verify,
    )


def write_skeleton(plan: InitPlan, *, force: bool = False) -> list[str]:
    """Write the plan's files, refusing to land on top of existing work.

    An existing EMPTY dir is fine (a human may have made it already); a non-empty one is
    refused unless ``force``, and even then no file is overwritten silently — ``force``
    only permits writing *into* a populated dir, never clobbering a path the plan owns.
    """
    root = plan.root
    if root.exists():
        if not root.is_dir():
            raise ProjectInitError(f"{root} exists and is not a directory")
        if any(root.iterdir()) and not force:
            raise ProjectInitError(
                f"{root} is not empty — refusing to write a skeleton over existing work "
                "(pass force=True/--force if you are sure)"
            )
    collisions = [rel for rel in plan.files if (root / rel).exists()]
    if collisions:
        raise ProjectInitError(
            f"{root} already contains skeleton files: {', '.join(sorted(collisions))} — "
            "refusing to overwrite them"
        )

    written: list[str] = []
    for rel, content in sorted(plan.files.items()):
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


# ---------------------------------------------------------------------------------------
# Git + GitHub + verification
# ---------------------------------------------------------------------------------------

def init_git(root: Path, *, run: CommandRunner = run_command) -> list[CommandResult]:
    """``git init`` + an initial commit, skipping init if the dir is already a repo."""
    results: list[CommandResult] = []
    if not (root / ".git").exists():
        results.append(run(["git", "init", "-b", "main"], root))
        if not results[-1].ok:
            return results
    results.append(run(["git", "add", "-A"], root))
    if not results[-1].ok:
        return results
    results.append(run(["git", "commit", "-m", "Initial project skeleton"], root))
    return results


def verify_skeleton(
    plan: InitPlan, *, run: CommandRunner = run_command
) -> tuple[bool, list[CommandResult]]:
    """Run the skeleton's declared verification commands in the new repo.

    These are the same commands phase 1 records on the adapter, so a red result here means
    the harness would be pointed at a repo whose quality gates cannot pass.
    """
    results = [run(cmd, plan.root) for cmd in plan.verify]
    return (all(r.ok for r in results), results)


def create_github_repo(
    plan: InitPlan,
    *,
    visibility: str = "private",
    run: CommandRunner = run_command,
) -> CommandResult:
    """Create the GitHub repo from the local one and push (``gh repo create --source``).

    Outward-facing and awkward to undo, so callers gate it: ``init_project`` only reaches
    here when explicitly asked AND the local skeleton verified green.
    """
    if visibility not in ("private", "public", "internal"):
        raise ProjectInitError(
            f"visibility must be private/public/internal, got {visibility!r}"
        )
    return run(
        ["gh", "repo", "create", plan.name, f"--{visibility}",
         "--source", ".", "--remote", "origin", "--push"],
        plan.root,
    )


def _tool_missing(*tools: str) -> list[str]:
    return [t for t in tools if shutil.which(t) is None]


# ---------------------------------------------------------------------------------------
# The orchestrating entry point
# ---------------------------------------------------------------------------------------

def init_project(
    raw_name: str,
    parent: Path,
    *,
    stack: str = DEFAULT_STACK,
    description: str = "",
    package: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    git: bool = True,
    verify: bool = True,
    create_repo: bool = False,
    visibility: str = "private",
    run: CommandRunner = run_command,
) -> dict[str, object]:
    """Plan, write, commit, verify, and (optionally) publish a phase-0 skeleton.

    Returns a JSON-able report. ``ok`` is False whenever any step this call was asked to
    perform failed — a skeleton that wrote fine but cannot pass its own verification is a
    failure, not a success with a warning.
    """
    plan = plan_project(
        raw_name, parent, stack=stack, description=description, package=package
    )
    report: dict[str, object] = {"ok": True, "dry_run": dry_run, **plan.as_dict()}

    if dry_run:
        report["would_create_repo"] = create_repo
        return report

    missing = _tool_missing(*(["git"] if git else []), *(["gh"] if create_repo else []))
    if missing:
        report["ok"] = False
        report["error"] = f"required tool(s) not on PATH: {', '.join(missing)}"
        return report

    report["written"] = write_skeleton(plan, force=force)

    if git:
        git_results = init_git(plan.root, run=run)
        report["git"] = [r.as_dict() for r in git_results]
        if not all(r.ok for r in git_results):
            report["ok"] = False
            report["error"] = "git init/commit failed — skeleton written but not committed"
            return report

    if verify:
        green, results = verify_skeleton(plan, run=run)
        report["verify"] = [r.as_dict() for r in results]
        report["verified"] = green
        if not green:
            report["ok"] = False
            report["error"] = (
                "the skeleton does not pass its own verification commands; the adapter "
                "would declare gates this repo cannot satisfy. Fix the repo before phase 1."
            )
            # Deliberately do NOT create the GitHub repo on red.
            return report

    if create_repo:
        result = create_github_repo(plan, visibility=visibility, run=run)
        report["github"] = result.as_dict()
        if not result.ok:
            report["ok"] = False
            report["error"] = "gh repo create failed — the local skeleton is intact"
            return report
        report["repo_visibility"] = visibility

    report["next"] = (
        f"uv run orchestrator-scaffold --detect {plan.root} --name {plan.name}"
    )
    return report
