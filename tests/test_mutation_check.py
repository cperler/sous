"""``orchestrator mutation-check`` fails loudly instead of reading as a survivor (#542 part d).

Reviewers' hand-rolled mutation loops kept reporting "survived" for runs that never ran a
test: zsh passed ``pytest $T`` as one nonexistent path, ``-n auto`` collected nothing, a grep
printed nothing. The command classifies each run from pytest's own summary and exit code,
and anything that is not a readable pass/fail result exits 2 as ``not_checked``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from orchestrator import cli
from orchestrator.mutation_check import (
    EXIT_NOT_CHECKED,
    Mutation,
    classify_run,
    mutation_check,
    parse_summary,
    split_quoted_command,
)

SOURCE = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """A tiny pytest project: calc.add plus one test that pins it."""
    (tmp_path / "calc.py").write_text(SOURCE)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    return tmp_path


def _pytest(*paths: str) -> list[str]:
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths]


# --- pure classification ----------------------------------------------------------------

@pytest.mark.parametrize(("output", "expected"), [
    ("..F.\n1 failed, 3 passed in 0.12s\n", {"failed": 1, "passed": 3}),
    ("==== 2 passed in 0.10s ====\n", {"passed": 2}),
    ("== 1 failed, 2 passed, 1 error, 3 warnings in 61.02s (0:01:01) ==",
     {"failed": 1, "passed": 2, "error": 1}),
    ("created: 8/8 workers\n8 workers [4 items]\n....\n4 passed in 1.31s\n", {"passed": 4}),
    ("\nno tests ran in 0.01s\n", {}),
    ("collected 0 items\n", None),
    ("", None),
])
def test_parse_summary(output: str, expected: dict | None) -> None:
    assert parse_summary(output) == expected


def test_a_count_printed_mid_run_is_not_the_summary() -> None:
    assert parse_summary("log: 3 passed earlier\nsomething else\n") is None


@pytest.mark.parametrize(("rc", "output", "status"), [
    (0, "3 passed in 0.1s", "passed"),
    (1, "1 failed, 2 passed in 0.1s", "failed"),
    (2, "1 error in 0.1s", "failed"),  # import broken by the mutant: a test went red
    (5, "no tests ran in 0.01s", "no_tests"),
    (0, "no tests ran in 0.01s", "no_tests"),
    (0, "3 skipped in 0.1s", "no_tests"),
    (4, "ERROR: file or directory not found: a b\n\nno tests ran in 0.01s", "usage_error"),
    (4, "", "usage_error"),
    (2, "Interrupted", "usage_error"),
    (0, "", "no_count"),
    (1, "some other runner's output", "no_count"),
    (1, "3 passed in 0.1s\nFAIL Required test coverage of 90% not reached", "runner_error"),
])
def test_classify_run(rc: int, output: str, status: str) -> None:
    assert classify_run(rc, output).status == status


def test_mutation_spec_keeps_colons_in_the_replacement() -> None:
    m = Mutation.parse("pkg/x.py:12:return {'a': 1}")
    assert (m.file, m.line, m.replacement) == ("pkg/x.py", 12, "return {'a': 1}")
    with pytest.raises(ValueError):
        Mutation.parse("pkg/x.py:zero:x")


# --- real runs --------------------------------------------------------------------------

def test_mutation_that_breaks_the_code_is_killed(proj: Path) -> None:
    """``+`` -> ``-`` keeps the file size and lands in the same second the baseline compiled
    calc.py, which is exactly when Python would reuse the stale .pyc and run the original."""
    mtime_ns = (proj / "calc.py").stat().st_mtime_ns
    code, report = mutation_check(_pytest("tests/test_calc.py"), cwd=proj,
                                  mutation=Mutation("calc.py", 2, "return a - b"))
    assert (code, report["outcome"]) == (0, "killed")
    assert report["mutation"] == {"file": "calc.py", "line": 2,
                                  "original": "    return a + b", "mutated": "    return a - b"}
    assert (proj / "calc.py").read_text() == SOURCE
    assert (proj / "calc.py").stat().st_mtime_ns == mtime_ns  # restored untouched


def test_mutation_the_tests_miss_survives(proj: Path) -> None:
    code, report = mutation_check(_pytest("tests/test_calc.py"), cwd=proj,
                                  mutation=Mutation("calc.py", 2, "return (a + b) * 1"))
    assert (code, report["outcome"]) == (0, "survived")
    assert report["baseline"]["passed"] == 1  # type: ignore[index]
    assert (proj / "calc.py").read_text() == SOURCE


def test_unsplit_path_list_is_not_checked_never_survived(proj: Path) -> None:
    """The zsh ``pytest $T`` case: two paths arrive as ONE argument."""
    code, report = mutation_check(_pytest("tests/test_calc.py tests/test_other.py"), cwd=proj,
                                  mutation=Mutation("calc.py", 2, "return (a + b) * 1"))
    assert code == EXIT_NOT_CHECKED
    assert report["outcome"] == "not_checked"
    assert (report["failed_run"], report["reason"]) == ("baseline", "usage_error")
    assert "mutated_run" not in report  # the mutation never ran on a bad baseline
    assert (proj / "calc.py").read_text() == SOURCE


def test_baseline_with_no_tests_collected_is_not_checked(proj: Path) -> None:
    (proj / "tests" / "test_empty.py").write_text("x = 1\n")
    code, report = mutation_check(_pytest("tests/test_empty.py"), cwd=proj)
    assert code == EXIT_NOT_CHECKED
    assert (report["failed_run"], report["reason"]) == ("baseline", "no_tests")


def test_command_printing_no_count_is_not_checked(proj: Path) -> None:
    code, report = mutation_check([sys.executable, "-c", "pass"], cwd=proj)
    assert code == EXIT_NOT_CHECKED
    assert (report["failed_run"], report["reason"]) == ("baseline", "no_count")


def test_mutated_run_that_collects_nothing_is_not_checked(proj: Path) -> None:
    """A mutant that makes pytest collect nothing (here the test file mutates itself away)
    is reported against the MUTATION run, and the file is still restored."""
    test_file = proj / "tests" / "test_calc.py"
    before = test_file.read_bytes()
    code, report = mutation_check(_pytest("tests/test_calc.py"), cwd=proj,
                                  mutation=Mutation("tests/test_calc.py", 4, "def helper_add():"))
    assert code == EXIT_NOT_CHECKED
    assert (report["failed_run"], report["reason"]) == ("mutation", "no_tests")
    assert test_file.read_bytes() == before


def test_hanging_mutant_times_out_and_is_restored(proj: Path) -> None:
    code, report = mutation_check(
        _pytest("tests/test_calc.py"), cwd=proj, timeout_s=5,
        mutation=Mutation("calc.py", 2, "import time; time.sleep(60); return a + b"),
    )
    assert code == EXIT_NOT_CHECKED
    assert (report["failed_run"], report["reason"]) == ("mutation", "timeout")
    assert report["mutated_run"]["duration_s"] < 30  # type: ignore[index]
    assert (proj / "calc.py").read_text() == SOURCE


def test_a_mutation_that_changes_nothing_is_refused(proj: Path) -> None:
    code, report = mutation_check(_pytest("tests/test_calc.py"), cwd=proj,
                                  mutation=Mutation("calc.py", 2, "return a + b"))
    assert code == EXIT_NOT_CHECKED
    assert "does not change" in str(report["reason"])


def test_a_mutation_that_breaks_the_syntax_is_not_checked_never_killed(proj: Path) -> None:
    """pred-night-1120 #621/#2136: a replacement aimed one line off broke the indentation, the
    run failed at collection, and the tool called it killed. It is refused before any run."""
    code, report = mutation_check(_pytest("tests/test_calc.py"), cwd=proj,
                                  mutation=Mutation("calc.py", 2, "return a - b"))
    assert report["outcome"] == "killed"  # control: an indented replacement is fine
    code, report = mutation_check(_pytest("tests/test_calc.py"), cwd=proj,
                                  mutation=Mutation("calc.py", 1, "return a - b"))
    assert code == EXIT_NOT_CHECKED
    assert report["failed_run"] == "mutation"
    assert "does not compile" in str(report["reason"])
    assert "baseline" not in report
    assert (proj / "calc.py").read_text() == SOURCE


# --- CLI --------------------------------------------------------------------------------

def test_cli_prints_json_and_exits_2_with_a_loud_stderr(proj: Path, capsys) -> None:
    rc = cli.main(["mutation-check", "-C", str(proj), "--mutate", "calc.py:2:return a - b",
                   "--", *_pytest("tests/test_calc.py tests/x.py")])
    out = capsys.readouterr()
    assert rc == EXIT_NOT_CHECKED
    assert json.loads(out.out)["outcome"] == "not_checked"
    assert "NOT CHECKED" in out.err and "not a surviving mutation" in out.err
    assert "file or directory not found" in out.err  # the runner's own words, not silence


def test_cli_reports_a_killed_mutant(proj: Path, capsys) -> None:
    rc = cli.main(["mutation-check", "-C", str(proj), "--mutate", "calc.py:2:return a - b",
                   "--", *_pytest("tests/test_calc.py")])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "killed"


def test_cli_splits_a_command_quoted_as_one_word(proj: Path, capsys) -> None:
    """pred-2041-2116: three reviewers passed ``-- "<test command>"`` as one word and every
    check read usage_error. The quoted command now runs as if written out word by word."""
    quoted = " ".join(_pytest("tests/test_calc.py"))
    rc = cli.main(["mutation-check", "-C", str(proj), "--mutate", "calc.py:2:return a - b",
                   "--", quoted])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "killed"


def test_split_leaves_separate_words_and_real_paths_alone(tmp_path: Path) -> None:
    words = ["pytest", "tests/a b.py"]
    assert split_quoted_command(words, cwd=tmp_path) == words
    assert split_quoted_command(["pytest"], cwd=tmp_path) == ["pytest"]
    (tmp_path / "my runner").write_text("")
    assert split_quoted_command(["my runner"], cwd=tmp_path) == ["my runner"]
    assert split_quoted_command(["uv run pytest -q"], cwd=tmp_path) == ["uv", "run", "pytest", "-q"]
    assert split_quoted_command(["pytest", "a.py b.py"], cwd=tmp_path) == ["pytest", "a.py b.py"]


def test_split_a_quoted_command_followed_by_test_paths(tmp_path: Path) -> None:
    """pred-maint-1118: the #2112 and #1533 reviewers passed ``-- "$T" tests/x.py`` -- the
    command quoted as one word, then a path -- and read usage_error, because only a LONE
    quoted word was split. The first word now splits whatever follows it."""
    got = split_quoted_command(["uv run python -m pytest -q", "tests/x.py"], cwd=tmp_path)
    assert got == ["uv", "run", "python", "-m", "pytest", "-q", "tests/x.py"]


def test_cli_splits_a_quoted_command_followed_by_a_path(proj: Path, capsys) -> None:
    quoted = " ".join(_pytest("tests/test_calc.py")[:-1])
    rc = cli.main(["mutation-check", "-C", str(proj), "--mutate", "calc.py:2:return a - b",
                   "--", quoted, "tests/test_calc.py"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "killed"


def test_review_prompt_names_the_command() -> None:
    """Both the single reviewer and the panel's find:tests lens read _MUTATION_CHECK_TRUST."""
    from orchestrator.stages import _MUTATION_CHECK_COMMAND, _MUTATION_CHECK_TRUST

    assert _MUTATION_CHECK_COMMAND in _MUTATION_CHECK_TRUST
    assert "mutation-check" in _MUTATION_CHECK_COMMAND
    assert "not a surviving mutation" in _MUTATION_CHECK_COMMAND
