"""`orchestrator gc` — list/prune long-lived checkpoint tags (issue #10).

Drives the CLI end to end (``orchestrator.cli.main``) against a real git repo in a
tmpdir, mirroring tests/test_checkpoint.py (real ``git tag``) and tests/test_cli.py
(rc==0 + parse the JSON emitted on stdout).
"""

from __future__ import annotations

import json
import subprocess

import pytest

from orchestrator.cli import main


def _git(cwd, *args, date=None) -> subprocess.CompletedProcess:
    env = None
    if date is not None:
        # Annotated-tag creatordate == tagger date; pin it so --sort=-creatordate
        # ordering is deterministic instead of same-second flaky.
        env = {"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date}
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
        env={**_base_env(), **env} if env else None,
    )


def _base_env() -> dict:
    import os

    return dict(os.environ)


def _tags(cwd, pattern="task/*") -> set[str]:
    out = _git(cwd, "tag", "-l", pattern).stdout
    return {t for t in out.splitlines() if t.strip()}


def _run(capsys, *argv) -> dict:
    rc = main(list(argv))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out)


@pytest.fixture
def repo(tmp_path):
    """A git repo with one commit and a handful of checkpoint tags to garbage-collect."""
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "f.txt").write_text("v1")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "c1")
    return r


def _tag(repo, name, date) -> None:
    assert _git(repo, "tag", "-a", "-m", name, name, date=date).returncode == 0


def test_dry_run_lists_candidates_but_deletes_nothing(repo, capsys) -> None:
    _tag(repo, "task/r1/t1/intake/0", "2026-07-01T00:00:00")
    _tag(repo, "task/r1/t1/implement/0", "2026-07-01T00:01:00")
    before = _tags(repo)

    out = _run(capsys, "gc", "--repo", str(repo))

    assert out["dry_run"] is True
    assert out["deleted"] == []
    assert set(out["candidates"]) == before
    assert _tags(repo) == before  # nothing actually removed


def test_prune_deletes_matching_checkpoint_tags(repo, capsys) -> None:
    _tag(repo, "task/r1/t1/intake/0", "2026-07-01T00:00:00")
    _tag(repo, "task/r1/t1/implement/0", "2026-07-01T00:01:00")
    _tag(repo, "v1.0", "2026-07-01T00:02:00")  # non-checkpoint tag must survive

    out = _run(capsys, "gc", "--repo", str(repo), "--prune")

    assert out["dry_run"] is False
    assert set(out["deleted"]) == {"task/r1/t1/intake/0", "task/r1/t1/implement/0"}
    assert _tags(repo) == set()  # both task/* tags gone
    assert _git(repo, "tag", "-l", "v1.0").stdout.strip() == "v1.0"  # untouched


def test_run_scope_only_prunes_that_runs_tags(repo, capsys) -> None:
    _tag(repo, "task/r1/t1/intake/0", "2026-07-01T00:00:00")
    _tag(repo, "task/r1/t1/implement/0", "2026-07-01T00:01:00")
    _tag(repo, "task/r2/t1/intake/0", "2026-07-01T00:02:00")

    out = _run(capsys, "--run", "r1", "gc", "--repo", str(repo), "--prune")

    assert out["scope"] == "task/r1/*"
    assert set(out["deleted"]) == {"task/r1/t1/intake/0", "task/r1/t1/implement/0"}
    assert _tags(repo) == {"task/r2/t1/intake/0"}  # r2 untouched


def test_keep_latest_keeps_newest_and_prunes_the_rest(repo, capsys) -> None:
    _tag(repo, "task/r1/t1/intake/0", "2026-07-01T00:00:00")     # oldest
    _tag(repo, "task/r1/t1/implement/0", "2026-07-01T00:01:00")  # middle
    _tag(repo, "task/r1/t1/test/0", "2026-07-01T00:02:00")       # newest

    out = _run(capsys, "gc", "--repo", str(repo), "--keep-latest", "1", "--prune")

    assert out["kept"] == ["task/r1/t1/test/0"]  # newest by creatordate
    assert set(out["deleted"]) == {"task/r1/t1/intake/0", "task/r1/t1/implement/0"}
    assert _tags(repo) == {"task/r1/t1/test/0"}
