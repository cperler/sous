"""Phase-0 skeleton generation (#367).

The behaviours worth pinning are the refusals and the ordering, not the file contents:
a skeleton that overwrites existing work, or that publishes a GitHub repo for a tree
that cannot pass its own gates, is the failure mode this module exists to prevent.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from orchestrator.cli import main
from orchestrator.project_init import (
    CommandResult,
    ProjectInitError,
    available_stacks,
    init_project,
    load_skeleton,
    normalize_name,
    package_name,
    plan_project,
    write_skeleton,
)


class FakeRunner:
    """Records every shelled command; returns a canned exit code per command prefix."""

    def __init__(self, failures: dict[str, int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.failures = failures or {}

    def __call__(self, argv: Sequence[str], cwd: Path) -> CommandResult:
        self.calls.append(list(argv))
        for prefix, code in self.failures.items():
            if " ".join(argv).startswith(prefix):
                return CommandResult(list(argv), code, "", f"boom: {prefix}")
        return CommandResult(list(argv), 0, "ok", "")

    def ran(self, prefix: str) -> bool:
        return any(" ".join(c).startswith(prefix) for c in self.calls)


# ---------------------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("prediction-markets", "prediction-markets"),
        ("Prediction Markets", "prediction-markets"),
        ("prediction_markets", "prediction-markets"),
        ("  Weird   Spacing  ", "weird-spacing"),
        ("a--b", "a-b"),
    ],
)
def test_normalize_name_accepts_what_humans_type(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "!!!", "-", "Ünicode"])
def test_normalize_name_refuses_unusable(raw: str) -> None:
    with pytest.raises(ProjectInitError):
        normalize_name(raw)


def test_package_name_refuses_a_python_keyword() -> None:
    # "class" is a legal repo name but an illegal import package — catch it at plan time
    # rather than at the first import in a generated file.
    with pytest.raises(ProjectInitError, match="not a valid Python identifier"):
        package_name("class")


def test_package_name_maps_hyphens() -> None:
    assert package_name("prediction-markets") == "prediction_markets"


# ---------------------------------------------------------------------------------------
# Skeleton + plan
# ---------------------------------------------------------------------------------------

def test_python_skeleton_is_available() -> None:
    assert "python" in available_stacks()


def test_unknown_stack_lists_the_known_ones() -> None:
    with pytest.raises(ProjectInitError, match="available: python"):
        load_skeleton("cobol")


def test_plan_substitutes_name_and_package(tmp_path: Path) -> None:
    plan = plan_project("prediction-markets", tmp_path, description="Market analysis.")
    assert plan.package == "prediction_markets"
    assert plan.root == tmp_path.resolve() / "prediction-markets"

    # The package path itself is templated, not just file contents.
    assert "src/prediction_markets/version.py" in plan.files
    assert "{{PACKAGE}}" not in json.dumps(plan.files)
    assert "{{NAME}}" not in json.dumps(plan.files)
    assert "{{DESCRIPTION}}" not in json.dumps(plan.files)

    assert "Market analysis." in plan.files["README.md"]
    assert 'name = "prediction-markets"' in plan.files["pyproject.toml"]
    assert "from prediction_markets import __version__" in plan.files["tests/test_version.py"]


def test_plan_declares_the_verify_commands_phase_one_will_record(tmp_path: Path) -> None:
    plan = plan_project("demo", tmp_path)
    rendered = [" ".join(c) for c in plan.verify]
    # These exact three are what USING.md phase 0 defines "done" as, and what the
    # generated adapter declares in phase 1.
    for command in ("uv run pytest", "uv run ruff check .", "uv run mypy"):
        assert command in rendered


def test_pyproject_gates_the_bare_mypy_invocation(tmp_path: Path) -> None:
    # A `[tool.mypy] files` entry is what makes the argument-free `uv run mypy` the
    # adapter shells out to actually check anything. It must cover BOTH trees: phase 1's
    # `orchestrator-scaffold --detect` writes the typecheck command as `uv run mypy .`,
    # so a narrower `files` would gate a different tree than the bare command checks.
    plan = plan_project("demo", tmp_path)
    assert "[tool.mypy]" in plan.files["pyproject.toml"]
    assert 'files = ["src", "tests"]' in plan.files["pyproject.toml"]


def test_gitignore_excludes_run_logs(tmp_path: Path) -> None:
    # runs/ is the durable audit trail and is local-only — committing it would be wrong.
    plan = plan_project("demo", tmp_path)
    assert "runs/" in plan.files[".gitignore"]


# ---------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------

def test_write_skeleton_creates_the_tree(tmp_path: Path) -> None:
    plan = plan_project("demo", tmp_path)
    written = write_skeleton(plan)
    assert "pyproject.toml" in written
    assert (plan.root / "src" / "demo" / "version.py").is_file()
    assert (plan.root / "tests" / "test_version.py").is_file()


def test_write_skeleton_refuses_a_non_empty_dir(tmp_path: Path) -> None:
    plan = plan_project("demo", tmp_path)
    plan.root.mkdir(parents=True)
    (plan.root / "existing.py").write_text("mine\n", encoding="utf-8")
    with pytest.raises(ProjectInitError, match="not empty"):
        write_skeleton(plan)
    assert (plan.root / "existing.py").read_text(encoding="utf-8") == "mine\n"


def test_force_writes_into_a_populated_dir_but_never_clobbers(tmp_path: Path) -> None:
    plan = plan_project("demo", tmp_path)
    plan.root.mkdir(parents=True)
    (plan.root / "notes.md").write_text("keep me\n", encoding="utf-8")
    write_skeleton(plan, force=True)
    assert (plan.root / "notes.md").read_text(encoding="utf-8") == "keep me\n"

    # A second pass collides with its own output — refused even under force.
    with pytest.raises(ProjectInitError, match="refusing to overwrite"):
        write_skeleton(plan, force=True)


def test_an_empty_existing_dir_is_fine(tmp_path: Path) -> None:
    plan = plan_project("demo", tmp_path)
    plan.root.mkdir(parents=True)
    assert write_skeleton(plan)


# ---------------------------------------------------------------------------------------
# init_project orchestration
# ---------------------------------------------------------------------------------------

def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    runner = FakeRunner()
    report = init_project("demo", tmp_path, dry_run=True, create_repo=True, run=runner)
    assert report["ok"] is True
    assert report["would_create_repo"] is True
    assert not (tmp_path / "demo").exists()
    assert runner.calls == []


def test_happy_path_commits_verifies_and_reports_next_step(tmp_path: Path) -> None:
    runner = FakeRunner()
    report = init_project("demo", tmp_path, run=runner)
    assert report["ok"] is True
    assert report["verified"] is True
    assert runner.ran("git init")
    assert runner.ran("git commit")
    assert runner.ran("uv run mypy")
    assert "orchestrator-scaffold --detect" in str(report["next"])
    # Not asked for, not done.
    assert not runner.ran("gh repo create")
    assert "github" not in report


def test_red_verification_fails_the_run_and_blocks_repo_creation(tmp_path: Path) -> None:
    # The load-bearing ordering: a tree that cannot pass its own gates must never be
    # published, because those gates become the adapter's contract in phase 1.
    runner = FakeRunner(failures={"uv run mypy": 1})
    report = init_project("demo", tmp_path, create_repo=True, run=runner)
    assert report["ok"] is False
    assert report["verified"] is False
    assert "does not pass its own verification" in str(report["error"])
    assert not runner.ran("gh repo create")


def test_failed_verification_reports_which_command_and_its_output(tmp_path: Path) -> None:
    runner = FakeRunner(failures={"uv run ruff": 2})
    report = init_project("demo", tmp_path, run=runner)
    failed = [r for r in report["verify"] if not r["ok"]]  # type: ignore[union-attr]
    assert len(failed) == 1
    assert failed[0]["cmd"] == "uv run ruff check ."
    assert "boom" in failed[0]["stderr"]


def test_repo_creation_is_opt_in_and_uses_the_requested_visibility(tmp_path: Path) -> None:
    runner = FakeRunner()
    report = init_project(
        "demo", tmp_path, create_repo=True, visibility="public", run=runner
    )
    assert report["ok"] is True
    created = [c for c in runner.calls if c[:3] == ["gh", "repo", "create"]]
    assert len(created) == 1
    assert "--public" in created[0]
    assert "--push" in created[0]
    assert report["repo_visibility"] == "public"


def test_bad_visibility_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProjectInitError, match="visibility must be"):
        init_project(
            "demo", tmp_path, create_repo=True, visibility="secret", run=FakeRunner()
        )


def test_git_failure_stops_before_verification(tmp_path: Path) -> None:
    runner = FakeRunner(failures={"git commit": 1})
    report = init_project("demo", tmp_path, run=runner)
    assert report["ok"] is False
    assert "not committed" in str(report["error"])
    assert not runner.ran("uv run pytest")


def test_no_git_skips_git_entirely(tmp_path: Path) -> None:
    runner = FakeRunner()
    report = init_project("demo", tmp_path, git=False, run=runner)
    assert report["ok"] is True
    assert not runner.ran("git")
    assert "git" not in report


# ---------------------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------------------

def test_cli_dry_run_needs_no_engine_or_project(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    # No --root, --run, or --project: at phase 0 none of them exist yet.
    code = main(["init-project", "prediction-markets", "--into", str(tmp_path), "--dry-run"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["package"] == "prediction_markets"
    assert "src/prediction_markets/version.py" in report["files"]


def test_cli_exits_non_zero_on_a_bad_name(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["init-project", "!!!", "--into", str(tmp_path), "--dry-run"])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
