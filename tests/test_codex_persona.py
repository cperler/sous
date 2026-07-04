"""Codex-native persona parity (#74).

claude reaches its stage persona via ``claude -p --agent <name>`` (the CLI reads the roster's
``.claude/agents/<name>.md``). codex has no ``--agent``; its persona convention is an
``AGENTS.md`` read from the working directory. These tests prove the codex transport resolves
the WorkItem's agent content and materializes it as ``<worktree>/AGENTS.md`` before dispatch —
composing (not clobbering) a project-shipped AGENTS.md via an idempotent marker section, swapping
the persona across stages, leaving files untouched when nothing resolves, and swallowing any
failure without breaking the call. Mirrors ``test_codex_continuity`` (fake runner, real tmp
worktrees) — the ``git`` calls delegate to real git so ``_codex_git_common_dir`` resolves.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from adapters.execution.transport import (
    RawResult,
    _compose_agents_md,
    _strip_frontmatter,
    codex_cli_transport,
    to_stage_result,
)
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem

_CODEX = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)
_START = "<!-- orchestrator:stage-persona:start -->"
_END = "<!-- orchestrator:stage-persona:end -->"

_REAL_RUN = subprocess.run  # captured before monkeypatch so git can delegate to the real thing


def _init_repo(root: Path) -> None:
    """A minimal real git repo so the transport's ``git rev-parse --git-common-dir`` (→ the
    repo_root candidate for ``.claude/agents``) resolves."""
    _REAL_RUN(["git", "init", "-q"], cwd=root, check=True)


def _agent(root: Path, name: str, body: str) -> None:
    d = root / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(body, encoding="utf-8")


def _events(thread_id: str = "th-1") -> str:
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": thread_id}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}),
    ])


def _codex_fake(calls: list, payloads: list[dict]):
    """``subprocess.run`` fake: delegates ``git`` to real git (so git-common-dir resolves),
    and fakes ``codex`` — recording argv, writing the queued structured output to the
    ``--output-last-message`` file, and returning the queued stdout/exit (last payload sticks)."""
    def fake_run(argv, **kw):
        if argv and argv[0] == "git":
            return _REAL_RUN(argv, **kw)
        calls.append(list(argv))
        p = payloads[min(len(calls) - 1, len(payloads) - 1)]
        if "--output-last-message" in argv and p.get("structured") is not None:
            target = argv[argv.index("--output-last-message") + 1]
            Path(target).write_text(json.dumps(p["structured"]), encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, p.get("returncode", 0), stdout=p.get("stdout", ""), stderr=p.get("stderr", "")
        )
    return fake_run


def _work(cwd: Path, *, agent: str | None = "python-backend-developer",
          stage: Stage = Stage.IMPLEMENT, session_ref: str | None = None) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="t1", stage=stage, prompt="do it",
        schema_ref="implement", model="gpt-5-codex", created_at="now", agent=agent,
        cwd=str(cwd), session_ref=session_ref, lane_policy=_CODEX,
    )


def _ok(calls):
    return _codex_fake(calls, [{"structured": {"ok": True}, "stdout": _events()}])


# --- resolution + materialization --------------------------------------------------------

def test_agent_content_resolved_and_written_with_markers(tmp_path, monkeypatch) -> None:
    _init_repo(tmp_path)
    _agent(tmp_path, "python-backend-developer",
           "---\nname: python-backend-developer\ndescription: d\n---\n\nYou implement Python.\n")
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _ok(calls))

    raw = codex_cli_transport()(_work(tmp_path))

    md = (tmp_path / "AGENTS.md").read_text()
    assert _START in md and _END in md
    assert "You implement Python." in md
    assert "name: python-backend-developer" not in md  # frontmatter stripped
    assert raw.persona_injected["agent"] == "python-backend-developer"
    assert raw.persona_injected["path"].endswith(".claude/agents/python-backend-developer.md")
    assert len(calls) == 1  # the codex call still went out


def test_existing_project_agents_md_preserved_with_section_appended(tmp_path, monkeypatch) -> None:
    _init_repo(tmp_path)
    _agent(tmp_path, "reviewer", "Review carefully.\n")
    (tmp_path / "AGENTS.md").write_text("# Project rules\n\nUse tabs.\n", encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _ok(calls))

    codex_cli_transport()(_work(tmp_path, agent="reviewer"))

    md = (tmp_path / "AGENTS.md").read_text()
    assert "# Project rules" in md and "Use tabs." in md   # project content preserved
    assert "Review carefully." in md                        # persona appended
    assert md.index("Use tabs.") < md.index("Review carefully.")  # appended AFTER the project's


def test_repeated_dispatch_replaces_not_stacks(tmp_path, monkeypatch) -> None:
    _init_repo(tmp_path)
    _agent(tmp_path, "reviewer", "Review carefully.\n")
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _ok(calls))

    t = codex_cli_transport()
    t(_work(tmp_path, agent="reviewer"))
    t(_work(tmp_path, agent="reviewer"))

    md = (tmp_path / "AGENTS.md").read_text()
    assert md.count(_START) == 1 and md.count(_END) == 1  # one section, not stacked
    assert md.count("Review carefully.") == 1


def test_persona_swaps_between_stages(tmp_path, monkeypatch) -> None:
    _init_repo(tmp_path)
    _agent(tmp_path, "impl", "Implement the change.\n")
    _agent(tmp_path, "rev", "Review the change.\n")
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _ok(calls))

    t = codex_cli_transport()
    t(_work(tmp_path, agent="impl", stage=Stage.IMPLEMENT))
    assert "Implement the change." in (tmp_path / "AGENTS.md").read_text()

    t(_work(tmp_path, agent="rev", stage=Stage.REVIEW))
    md = (tmp_path / "AGENTS.md").read_text()
    assert "Review the change." in md          # new persona present
    assert "Implement the change." not in md   # old persona swapped out
    assert md.count(_START) == 1               # still one section


def test_persona_refreshed_on_resume_dispatch(tmp_path, monkeypatch) -> None:
    _init_repo(tmp_path)
    _agent(tmp_path, "reviewer", "Review the change.\n")
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _ok(calls))

    raw = codex_cli_transport()(_work(tmp_path, agent="reviewer", session_ref="th-1"))

    assert calls[0][:4] == ["codex", "exec", "resume", "th-1"]  # a resumed dispatch
    md = (tmp_path / "AGENTS.md").read_text()
    assert "Review the change." in md  # AGENTS.md still refreshed before the resumed call
    assert raw.persona_injected["agent"] == "reviewer"


# --- no-op + failure paths ---------------------------------------------------------------

def test_no_agent_writes_nothing_and_leaves_existing_untouched(tmp_path, monkeypatch) -> None:
    _init_repo(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Project rules\n", encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _ok(calls))

    raw = codex_cli_transport()(_work(tmp_path, agent=None))

    assert (tmp_path / "AGENTS.md").read_text() == "# Project rules\n"  # untouched
    assert raw.persona_injected is None
    assert len(calls) == 1  # the call still proceeded


def test_unresolved_agent_writes_nothing_and_leaves_existing_untouched(tmp_path, monkeypatch) -> None:
    _init_repo(tmp_path)  # no .claude/agents dir, and the name isn't in the packaged kit either
    (tmp_path / "AGENTS.md").write_text("# Project rules\n", encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _ok(calls))

    raw = codex_cli_transport()(_work(tmp_path, agent="no-such-agent-anywhere"))

    assert (tmp_path / "AGENTS.md").read_text() == "# Project rules\n"  # untouched
    assert raw.persona_injected is None


def test_injection_failure_is_swallowed_call_proceeds_and_is_noted(tmp_path, monkeypatch) -> None:
    _init_repo(tmp_path)
    _agent(tmp_path, "reviewer", "Review.\n")
    (tmp_path / "AGENTS.md").mkdir()  # a DIRECTORY named AGENTS.md -> read/write raises
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _ok(calls))

    raw = codex_cli_transport()(_work(tmp_path, agent="reviewer"))

    assert raw.exit_code == 0 and raw.structured_output == {"ok": True}  # call proceeded
    assert len(calls) == 1
    assert "error" in raw.persona_injected  # the swallowed failure is noted for observability
    assert raw.persona_injected["agent"] == "reviewer"


# --- observability + pure helpers --------------------------------------------------------

def test_persona_injected_flows_to_stage_result(tmp_path) -> None:
    raw = RawResult({"ok": True}, persona_injected={"agent": "reviewer", "path": "/x.md"},
                    invocation="codex exec (fake)")
    sr = to_stage_result(_work(tmp_path), raw, ResultStatus.SUCCESS,
                         mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)
    assert sr.persona_injected == {"agent": "reviewer", "path": "/x.md"}


def test_strip_frontmatter() -> None:
    assert _strip_frontmatter("---\nname: x\ndescription: d\n---\n\nBody here.\n") == "Body here."
    assert _strip_frontmatter("No frontmatter at all.\n") == "No frontmatter at all."
    # a mid-file `---` horizontal rule is NOT treated as frontmatter (must open on line 1)
    assert _strip_frontmatter("Intro line\n\n---\n\nMore text").startswith("Intro line")


def test_compose_agents_md_new_and_upsert() -> None:
    # No existing file -> the section is the whole file.
    fresh = _compose_agents_md(None, "Persona A")
    assert fresh.startswith(_START) and "Persona A" in fresh and _END in fresh
    # Existing project file -> persona appended after it.
    with_existing = _compose_agents_md("# Rules\n", "Persona A")
    assert with_existing.startswith("# Rules") and "Persona A" in with_existing
    # Re-compose replaces the section in place (idempotent, no stacking).
    replaced = _compose_agents_md(with_existing, "Persona B")
    assert replaced.count(_START) == 1 and "Persona B" in replaced and "Persona A" not in replaced
