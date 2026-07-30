"""#272: the per-stage tool posture — REVIEW may not write the tree it is reviewing.

The gap this closes: every headless dispatch ran with `--dangerously-skip-permissions` and no
tool gating, so a reviewer had exactly the implementer's write authority in the same worktree.
"Reads and reports" was a prompt convention, not an enforced posture — and since #73's panel,
one review is up to 12 agents holding that authority.

The load-bearing properties under test:

* the posture is DECLARED per stage in the engine's provider-neutral vocabulary and attached
  only on model lanes — never a claude tool name inside `orchestrator/`;
* each transport TRANSLATES it (claude `--disallowedTools`, codex `--sandbox read-only` on
  BOTH the fresh and resume call shapes, so continuity can't silently revert the posture);
* an unset posture leaves every argv and every content_hash byte-identical to pre-#272;
* a panel's finders and verifiers INHERIT it (the 12-agent hole);
* a lane that cannot enforce says so, and the engine says so out loud once per dispatch.
"""

from __future__ import annotations

import json
import subprocess

from adapters.execution.base import Registry, default_registry
from adapters.execution.review_panel import _sub_item, run_review_panel
from adapters.execution.runners import build_registry
from adapters.execution.transport import (
    RawResult,
    claude_cli_transport,
    codex_cli_transport,
)
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.routing import Router
from orchestrator.schemas.enums import ExecutionMode, Provider, Stage
from orchestrator.schemas.work import (
    FinderSpec,
    LanePolicy,
    ReviewPlan,
    ToolPolicy,
    WorkItem,
    compute_content_hash,
)
from orchestrator.stages import STAGE_SPECS
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

H = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
READ_ONLY = ToolPolicy(allow_file_writes=False)


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _work(**kw) -> WorkItem:
    args: dict = dict(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.REVIEW, prompt="review it",
        schema_ref="review", model="claude-opus-5", created_at="now", lane_policy=H,
    )
    args.update(kw)
    return WorkItem.create(**args)


def _drive_to_review(eng, *, run="r1", task="t1"):
    """Advance a task until REVIEW is the dispatched stage; return that WorkItem."""
    while (w := eng.next_work(run, task)) is not None:
        if w.stage is Stage.REVIEW:
            return w
        eng.record(run, make_result(w))
    raise AssertionError("never reached REVIEW")


def _stub_run(calls: list):
    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"result": "ok"}), stderr="")
    return fake_run


# --- the vocabulary is provider-neutral ---------------------------------------------------

def test_tool_policy_defaults_to_the_historical_everything_allowed_posture() -> None:
    permissive = ToolPolicy()
    assert permissive.allow_file_writes and permissive.allow_command_execution
    # REVIEW's posture: writes denied, execution retained (the issue's explicit trade-off —
    # an adversarial verifier refutes a finding by RUNNING the suite).
    assert READ_ONLY.allow_command_execution


def test_only_review_declares_a_posture_and_it_denies_writes() -> None:
    assert STAGE_SPECS[Stage.REVIEW].tool_policy == READ_ONLY
    for stage, spec in STAGE_SPECS.items():
        if stage is not Stage.REVIEW:
            assert spec.tool_policy is None, f"{stage.value} must keep the write posture"


# --- the engine seam ----------------------------------------------------------------------

def test_review_dispatch_carries_the_posture_and_no_other_stage_does(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    seen: dict[Stage, ToolPolicy | None] = {}
    while (w := eng.next_work("r1", "t1")) is not None:
        seen[w.stage] = w.tool_policy
        eng.record("r1", make_result(w))
    assert seen[Stage.REVIEW] == READ_ONLY
    assert seen[Stage.REVIEW] is not None and not seen[Stage.REVIEW].allow_file_writes
    # implement legitimately writes; test fixes regressions; deliver pushes.
    for stage in (Stage.SCOPE, Stage.IMPLEMENT, Stage.TEST, Stage.DELIVER):
        assert seen[stage] is None, f"{stage.value} must not be write-denied"
    assert seen[Stage.INTAKE] is None  # deterministic ENGINE lane: no model, no toolset


def test_an_unregistered_lane_counts_as_not_enforcing(tmp_path, project) -> None:
    """The conservative reading: a lane whose descriptor the engine cannot even find must make
    the gap visible, not assume protection it has no evidence for."""
    eng = _engine(
        tmp_path, project, registry=Registry(),
        router=Router(execution_mode=ExecutionMode.HEADLESS, orchestrator_provider=Provider.CLAUDE),
    )
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _drive_to_review(eng)
    assert w.tool_policy == READ_ONLY
    assert len(_unenforced_events(eng)) == 1


# --- content_hash: dispatch metadata, not content -----------------------------------------

def test_posture_is_excluded_from_content_hash(tmp_path, project) -> None:
    """The posture is derived from the stage and the lane, both already hashed — so no two
    dispatches of the same work can legitimately disagree, and excluding it keeps an in-flight
    pre-#272 REVIEW lease verifiable on record()."""
    policed = _work(tool_policy=READ_ONLY)
    assert policed.tool_policy == READ_ONLY
    assert policed.content_hash == _work().content_hash
    assert policed.content_hash == compute_content_hash(
        stage=policed.stage, prompt=policed.prompt, schema_ref=policed.schema_ref,
        model=policed.model, lane_policy=policed.lane_policy, attempt=policed.attempt,
    )


def test_engine_review_hash_matches_the_policy_free_formula(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _drive_to_review(eng)
    assert w.tool_policy is not None
    assert w.content_hash == compute_content_hash(
        stage=w.stage, prompt=w.prompt, schema_ref=w.schema_ref, model=w.model,
        lane_policy=w.lane_policy, attempt=w.attempt, effort=w.effort, plan=w.plan,
    )
    eng.record("r1", make_result(w))  # the lease still verifies


# --- claude translation -------------------------------------------------------------------

def test_claude_argv_denies_the_write_tools_but_keeps_bash(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    claude_cli_transport()(_work(tool_policy=READ_ONLY))
    argv = calls[0]
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert denied == ["Write", "Edit", "NotebookEdit"]
    # Command execution is retained deliberately: the verifier must be able to run the suite.
    assert "Bash" not in denied
    # And the non-interactive gating stays — a permission prompt would hang a headless run.
    assert "--dangerously-skip-permissions" in argv


def test_claude_argv_denies_bash_when_the_posture_denies_execution(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    claude_cli_transport()(_work(
        tool_policy=ToolPolicy(allow_file_writes=False, allow_command_execution=False)
    ))
    argv = calls[0]
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert denied == ["Write", "Edit", "NotebookEdit", "Bash", "BashOutput", "KillShell"]


def test_claude_argv_without_a_posture_is_byte_identical(monkeypatch) -> None:
    """Zero behavior change off the policed stage: EXACTLY the pre-#272 argv."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    claude_cli_transport()(_work())
    w = _work()
    assert calls[0] == ["claude", "-p", w.prompt, "--model", w.model,
                        "--dangerously-skip-permissions", "--output-format", "json"]
    # a fully permissive policy denies nothing, so it must not add a flag either
    calls.clear()
    claude_cli_transport()(_work(tool_policy=ToolPolicy()))
    assert "--disallowedTools" not in calls[0]


# --- codex translation (fresh AND resume) -------------------------------------------------

def test_codex_fresh_call_goes_read_only(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    codex_cli_transport()(_work(model="gpt-5.5", tool_policy=READ_ONLY, cwd=None))
    argv = calls[0]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--full-auto" not in argv
    assert "--add-dir" not in argv  # a writable grant would contradict the posture
    assert 'approval_policy="never"' in argv  # a denial fails fast, never stalls


def test_codex_resume_keeps_the_posture(monkeypatch) -> None:
    """The posture must survive session continuity — a stage's SECOND call is where a
    write-sandbox default would quietly reappear."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    codex_cli_transport()(
        _work(model="gpt-5.5", tool_policy=READ_ONLY, session_ref="thread-1", cwd=None)
    )
    argv = calls[0]
    assert argv[:4] == ["codex", "exec", "resume", "thread-1"]
    assert 'sandbox_mode="read-only"' in argv
    assert 'sandbox_mode="workspace-write"' not in argv
    assert not any("writable_roots" in str(a) for a in argv)


def test_codex_argv_without_a_posture_is_unchanged(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    codex_cli_transport()(_work(model="gpt-5.5", cwd=None))
    assert "--full-auto" in calls[0] and "--sandbox" not in calls[0]
    calls.clear()
    codex_cli_transport()(_work(model="gpt-5.5", session_ref="thread-1", cwd=None))
    assert 'sandbox_mode="workspace-write"' in calls[0]
    assert 'sandbox_mode="read-only"' not in calls[0]


# --- panel inheritance: the 12-agent hole -------------------------------------------------

def test_sub_item_inherits_the_posture() -> None:
    w = _work(tool_policy=READ_ONLY, session_ref="sess", checkpoint_tag="ckpt")
    sub = _sub_item(w, phase="find:code", prompt="p", schema_ref="review_findings", agent=None)
    assert sub.tool_policy == READ_ONLY  # NOT on the strip list
    assert sub.session_ref is None and sub.checkpoint_tag is None  # ...unlike these


def test_every_finder_and_verifier_carries_the_parents_posture() -> None:
    """A FULL panel is up to 4 finders + 8 verifiers; a strip-list edit that dropped the
    posture would re-open the hole for all of them at once."""
    plan = ReviewPlan(
        finders=tuple(
            FinderSpec(lens=lens, prompt=f"## {lens}", agent=None, schema_ref="review_findings")
            for lens in ("find:code", "find:spec")
        ),
        verify_template="## VERIFY\n{finding}\n{diff_hint}\n",
        verify_schema_ref="review_verdict",
        dedupe_rule="fingerprint-v1",
    )
    seen: list[ToolPolicy | None] = []
    phases: list[str] = []

    def transport(work: WorkItem) -> RawResult:
        seen.append(work.tool_policy)
        phases.append(work.phase or "")
        if str(work.phase).startswith("find:"):
            payload = {"findings": [{"severity": "critical", "file": "a.py", "line": 1,
                                     "description": f"boom from {work.phase}"}]}
        else:
            payload = {"fingerprint": "x", "verdict": "upheld", "rationale": "stands"}
        return RawResult(payload, exit_code=0)

    run_review_panel(_work(tool_policy=READ_ONLY, plan=plan, cwd=None), transport)
    assert any(p.startswith("find:") for p in phases)
    assert any(p.startswith("verify:") for p in phases)
    assert seen and all(p == READ_ONLY for p in seen), phases


# --- honesty: a lane that cannot enforce says so ------------------------------------------

def test_headless_lanes_declare_enforcement() -> None:
    reg = build_registry(include_interactive=True)
    for provider in (Provider.CLAUDE, Provider.CODEX):
        lane = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=provider)
        assert reg.describe(lane).enforces_tool_policy, provider
    # the interactive shim's agent() call takes no tool restriction (#262)
    interactive = LanePolicy(execution_mode=ExecutionMode.INTERACTIVE, provider=Provider.CLAUDE)
    assert not reg.describe(interactive).enforces_tool_policy
    assert not default_registry().describe(interactive).enforces_tool_policy


def _unenforced_events(eng, run="r1") -> list[dict]:
    return [e for e in eng.store.read_events(run) if e.get("type") == "tool_policy_unenforced"]


def test_unenforced_posture_warns_exactly_once_on_the_interactive_lane(tmp_path, project) -> None:
    eng = _engine(
        tmp_path, project, registry=default_registry(),
        router=Router(execution_mode=ExecutionMode.INTERACTIVE,
                      orchestrator_provider=Provider.CLAUDE),
    )
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _drive_to_review(eng)
    assert w.tool_policy == READ_ONLY  # attached anyway — the runner may still honor it
    events = _unenforced_events(eng)
    assert len(events) == 1
    ev = events[0]
    assert ev["stage"] == Stage.REVIEW.value
    assert ev["severity"] == "warning"
    assert ev["lane"] == "interactive:claude"
    assert ev["work_item_id"] == w.id
    assert ev["policy"]["allow_file_writes"] is False


def test_no_warning_on_a_lane_that_enforces(tmp_path, project) -> None:
    eng = _engine(
        tmp_path, project, registry=build_registry(),
        router=Router(execution_mode=ExecutionMode.HEADLESS, orchestrator_provider=Provider.CLAUDE),
    )
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _drive_to_review(eng)
    assert w.lane_policy.execution_mode is ExecutionMode.HEADLESS
    assert w.tool_policy is not None
    assert _unenforced_events(eng) == []
