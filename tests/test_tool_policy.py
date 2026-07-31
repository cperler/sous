"""The per-stage, per-lane tool posture: #272 (REVIEW), widened and decided by #327.

#272: REVIEW may not write the tree it is reviewing.

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

#327 folded the three views #272 left open, and their properties are tested below too:

* #303 — SCOPE plans, it does not edit, so it declares the same posture as REVIEW, translated
  on BOTH provider lanes;
* #304 — `--dangerously-skip-permissions` is no longer an unconditional constant in
  `transport.py` but a lane-declared default that a write-denying stage tightens, so a
  read-only stage is never handed blanket permission (and an unstamped dispatch still bypasses,
  keeping every pre-#304 argv byte-identical);
* #302 — interactive×claude keeps `enforces_tool_policy=False` (its `agent()` call takes no
  tool restriction) and the degradation is now explicit AT THE DISPATCH: the posture is stated
  in-band in the prompt, not only after the fact in an event.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from adapters.execution import transport
from adapters.execution.base import Registry, default_registry
from adapters.execution.review_panel import _sub_item, run_review_panel
from adapters.execution.runners import build_registry
from adapters.execution.transport import (
    RawResult,
    claude_cli_transport,
    codex_cli_transport,
    resolve_permission_posture,
)
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.routing import Router
from orchestrator.schemas.enums import ExecutionMode, PermissionPosture, Provider, Stage
from orchestrator.schemas.work import (
    FinderSpec,
    LanePolicy,
    ReviewPlan,
    ToolPolicy,
    WorkItem,
    compute_content_hash,
)
from orchestrator.stages import STAGE_SPECS, render_prompt
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

H = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
READ_ONLY = ToolPolicy(allow_file_writes=False)
# The stages that read and report rather than write: REVIEW (#272) and SCOPE (#303).
_READING_STAGES = (Stage.SCOPE, Stage.REVIEW)


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _work(**kw) -> WorkItem:
    args: dict = dict(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.REVIEW, prompt="review it",
        schema_ref="review", model="claude-opus-5", created_at="now", lane_policy=H,
    )
    args.update(kw)
    return WorkItem.create(**args)


def _drive_to(eng, stage: Stage, *, run="r1", task="t1"):
    """Advance a task until ``stage`` is the dispatched stage; return that WorkItem."""
    while (w := eng.next_work(run, task)) is not None:
        if w.stage is stage:
            return w
        eng.record(run, make_result(w))
    raise AssertionError(f"never reached {stage.value}")


def _drive_to_review(eng, *, run="r1", task="t1"):
    return _drive_to(eng, Stage.REVIEW, run=run, task=task)


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


def test_the_reading_stages_declare_a_posture_and_it_denies_writes() -> None:
    """#303: SCOPE joined REVIEW. Both read the repo and return a document; the stages that
    legitimately mutate the tree (implement/test/deliver) must keep the write posture."""
    for stage in _READING_STAGES:
        assert STAGE_SPECS[stage].tool_policy == READ_ONLY, stage.value
    for stage, spec in STAGE_SPECS.items():
        if stage not in _READING_STAGES:
            assert spec.tool_policy is None, f"{stage.value} must keep the write posture"


# --- the engine seam ----------------------------------------------------------------------

def test_reading_dispatches_carry_the_posture_and_writing_stages_do_not(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    seen: dict[Stage, ToolPolicy | None] = {}
    postures: dict[Stage, PermissionPosture | None] = {}
    while (w := eng.next_work("r1", "t1")) is not None:
        seen[w.stage] = w.tool_policy
        postures[w.stage] = w.permission_posture
        eng.record("r1", make_result(w))
    for stage in _READING_STAGES:
        assert seen[stage] == READ_ONLY, stage.value
        # #304: a write-denied stage loses blanket permission with the write tools.
        assert postures[stage] is PermissionPosture.RESTRICTED, stage.value
    # implement legitimately writes; test fixes regressions; deliver pushes.
    for stage in (Stage.IMPLEMENT, Stage.TEST, Stage.DELIVER):
        assert seen[stage] is None, f"{stage.value} must not be write-denied"
        assert postures[stage] is PermissionPosture.BYPASS, stage.value
    assert seen[Stage.INTAKE] is None  # deterministic ENGINE lane: no model, no toolset
    assert postures[Stage.INTAKE] is None  # ...and so no permission gate to set either


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
    # One per posture-bearing dispatch: SCOPE and REVIEW (#303 widened the posture).
    assert [e["stage"] for e in _unenforced_events(eng)] == [Stage.SCOPE.value, Stage.REVIEW.value]


# --- content_hash: dispatch metadata, not content -----------------------------------------

def test_posture_is_excluded_from_content_hash(tmp_path, project) -> None:
    """The posture is derived from the stage and the lane, both already hashed — so no two
    dispatches of the same work can legitimately disagree, and excluding it keeps an in-flight
    pre-#272 REVIEW lease verifiable on record()."""
    policed = _work(tool_policy=READ_ONLY)
    assert policed.tool_policy == READ_ONLY
    assert policed.content_hash == _work().content_hash
    assert policed.model_copy(update={"workspace_isolated": True}).content_hash == \
        policed.content_hash
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
    # #304: and the read-only stage does NOT get blanket permission with it. Instead the
    # tools its posture still allows are pre-granted explicitly, so the dispatch never has to
    # answer a prompt (probed: an unlisted tool is refused in-band, it does not stall).
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--allowedTools") + 1].split(",") == ["Bash", "BashOutput", "KillShell"]


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


def test_codex_isolated_review_uses_writable_sandbox_fresh_and_resume(monkeypatch) -> None:
    """#301: the coarse sandbox may write only after the runner moved REVIEW off the live
    tree, so pytest/build caches work on both call shapes without dropping ToolPolicy."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    isolated = _work(
        model="gpt-5.5", tool_policy=READ_ONLY, cwd=None, workspace_isolated=True
    )
    codex_cli_transport()(isolated)
    assert "--full-auto" in calls[0]
    assert "--sandbox" not in calls[0]

    calls.clear()
    codex_cli_transport()(isolated.model_copy(update={"session_ref": "thread-1"}))
    assert 'sandbox_mode="workspace-write"' in calls[0]
    assert 'sandbox_mode="read-only"' not in calls[0]


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
    # Exactly one per affected dispatch — SCOPE and REVIEW, not one per run and not two for
    # either stage.
    assert [e["stage"] for e in events] == [Stage.SCOPE.value, Stage.REVIEW.value]
    ev = events[-1]
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


# --- #303: SCOPE carries the posture, translated on BOTH providers ------------------------

def _scope_work(tmp_path, project) -> WorkItem:
    """The REAL engine-emitted SCOPE dispatch — not a hand-built WorkItem — so the per-lane
    argv assertions below are anchored to what the engine actually sends."""
    eng = _engine(tmp_path, project, registry=build_registry(),
                  router=Router(execution_mode=ExecutionMode.HEADLESS,
                                orchestrator_provider=Provider.CLAUDE))
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    return _drive_to(eng, Stage.SCOPE)


def test_scope_dispatch_on_claude_denies_writes_keeps_bash_and_drops_the_bypass(
    tmp_path, project, monkeypatch
) -> None:
    """#303 on the claude lane: SCOPE plans, so its write tools are gone — while the reading
    and command tools it scopes WITH survive."""
    w = _scope_work(tmp_path, project)
    assert w.tool_policy == READ_ONLY
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    claude_cli_transport()(w)
    argv = calls[0]
    assert argv[argv.index("--disallowedTools") + 1].split(",") == ["Write", "Edit", "NotebookEdit"]
    assert argv[argv.index("--allowedTools") + 1].split(",") == ["Bash", "BashOutput", "KillShell"]
    assert "--dangerously-skip-permissions" not in argv


def test_scope_dispatch_on_codex_goes_read_only_fresh_and_on_resume(
    tmp_path, project, monkeypatch
) -> None:
    """#303 on the codex lane: the sandbox is the only enforcement primitive there, and it
    must hold on the stage's SECOND call too — session continuity is where a write posture
    would quietly reappear."""
    w = _scope_work(tmp_path, project)
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    codex_cli_transport()(w.model_copy(update={"model": "gpt-5.5", "cwd": None}))
    assert calls[0][calls[0].index("--sandbox") + 1] == "read-only"
    assert "--full-auto" not in calls[0]
    calls.clear()
    codex_cli_transport()(
        w.model_copy(update={"model": "gpt-5.5", "cwd": None, "session_ref": "thread-1"})
    )
    assert 'sandbox_mode="read-only"' in calls[0]
    assert 'sandbox_mode="workspace-write"' not in calls[0]


# --- #304: the permission gate is a lane/stage decision, not a constant --------------------

def test_the_bypass_flag_is_gone_from_the_transport_source() -> None:
    """The acceptance criterion stated structurally: `transport.py` may still NAME the flag
    (BYPASS translates to it), but never unconditionally — every emission sits behind the
    resolved posture. Guards against a future edit re-hardcoding it into the argv literal."""
    src = Path(transport.__file__).read_text(encoding="utf-8")
    argv_literals = [
        line for line in src.splitlines()
        if "--dangerously-skip-permissions" in line and "argv" in line
    ]
    assert argv_literals == [], argv_literals
    # It survives in exactly one place: the BYPASS branch of the permission translation.
    emitting = [ln.strip() for ln in src.splitlines()
                if 'return ["--dangerously-skip-permissions"]' in ln]
    assert len(emitting) == 1


def test_unstamped_and_permissive_dispatches_still_bypass(monkeypatch) -> None:
    """The fallback that keeps every pre-#304 argv byte-identical: no tool posture and no
    stamp (a direct-transport caller, or a WorkItem loaded from a pre-#304 doc) => BYPASS."""
    assert resolve_permission_posture(_work()) is PermissionPosture.BYPASS
    assert resolve_permission_posture(_work(tool_policy=ToolPolicy())) is PermissionPosture.BYPASS
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    claude_cli_transport()(_work(stage=Stage.IMPLEMENT, schema_ref="implement"))
    assert "--dangerously-skip-permissions" in calls[0]
    assert "--allowedTools" not in calls[0]


def test_a_stage_posture_tightens_a_bypass_stamp_but_a_stamp_never_loosens_one() -> None:
    """Monotone in tightness. A mis-stamped BYPASS cannot hand a write-denied stage blanket
    permission — the resolution reads the stage posture first."""
    assert resolve_permission_posture(
        _work(tool_policy=READ_ONLY, permission_posture=PermissionPosture.BYPASS)
    ) is PermissionPosture.RESTRICTED
    # ...and a lane that declares RESTRICTED keeps it on a stage that declares no posture.
    assert resolve_permission_posture(
        _work(permission_posture=PermissionPosture.RESTRICTED)
    ) is PermissionPosture.RESTRICTED


def test_a_lane_can_declare_a_non_bypass_default_for_every_stage(
    tmp_path, project, monkeypatch
) -> None:
    """#304's actual ask: a lane that must NOT hold blanket permission (a shared/production
    checkout) declares it once on its descriptor, and every stage on that lane — including the
    writing ones — dispatches without the bypass flag."""
    reg = build_registry()
    reg.register_external(
        reg.describe(H).model_copy(update={"permission_posture": PermissionPosture.RESTRICTED})
    )
    eng = _engine(tmp_path, project, registry=reg,
                  router=Router(execution_mode=ExecutionMode.HEADLESS,
                                orchestrator_provider=Provider.CLAUDE))
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _drive_to(eng, Stage.IMPLEMENT)
    assert w.tool_policy is None  # implement still gets its write tools...
    assert w.permission_posture is PermissionPosture.RESTRICTED  # ...but not blanket permission
    # ...and that is what the ARGV must actually say. Withholding the blanket grant must not
    # silently withhold the stage's own authority: with no TTY to answer a permission prompt,
    # a tool that is neither pre-granted nor denied is the one shape this posture must never
    # emit, so a writing stage on a RESTRICTED lane pre-grants its write tools explicitly.
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    claude_cli_transport()(w)
    argv = calls[0]
    assert "--dangerously-skip-permissions" not in argv
    granted = argv[argv.index("--allowedTools") + 1].split(",")
    assert set(granted) == {"Write", "Edit", "NotebookEdit", "Bash", "BashOutput", "KillShell"}
    # Nothing is denied (the stage declares no posture), so grant+deny cover the vocabulary.
    assert "--disallowedTools" not in argv


def test_a_lane_level_restriction_does_not_read_only_codex(monkeypatch) -> None:
    """The codex half of the same case: `codex exec` never emits a blanket-permission grant to
    withhold, and read-only'ing a WRITING stage would break it. Only a write-denying stage
    posture reaches the sandbox."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    codex_cli_transport()(_work(
        model="gpt-5.5", cwd=None, stage=Stage.IMPLEMENT, schema_ref="implement",
        permission_posture=PermissionPosture.RESTRICTED,
    ))
    assert "--full-auto" in calls[0] and "--sandbox" not in calls[0]


def test_permission_posture_is_excluded_from_content_hash() -> None:
    """Same argument as the tool policy: derived from the stage and the lane, both already
    hashed — so an in-flight lease stamped before #304 still verifies on record()."""
    stamped = _work(permission_posture=PermissionPosture.RESTRICTED)
    assert stamped.permission_posture is PermissionPosture.RESTRICTED
    assert stamped.content_hash == _work().content_hash


# --- #302: the decided degradation path on the lane that cannot enforce -------------------

def test_an_unenforced_posture_is_stated_in_band_to_the_model(tmp_path, project) -> None:
    """The decision: interactive×claude keeps `enforces_tool_policy=False` (its `agent()` call
    takes no tool restriction), so the posture is stated in the PROMPT there — the only place
    it can be stated at all. A warning event alone would tell the human afterwards while the
    dispatch ran as if no posture existed."""
    eng = _engine(
        tmp_path, project, registry=default_registry(),
        router=Router(execution_mode=ExecutionMode.INTERACTIVE,
                      orchestrator_provider=Provider.CLAUDE),
    )
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    for stage in _READING_STAGES:
        w = _drive_to(eng, stage)
        assert "cannot enforce it" in w.prompt, stage.value
        assert "Do NOT create, modify, or delete any file" in w.prompt, stage.value
        eng.record("r1", make_result(w))


def test_the_directive_is_absent_where_the_posture_is_really_enforced(tmp_path, project) -> None:
    """It is a DEGRADATION path, not a belt-and-suspenders prompt: on a lane whose transport
    removes the tools, the prompt stays byte-identical to the pre-#302 one."""
    eng = _engine(tmp_path, project, registry=build_registry(),
                  router=Router(execution_mode=ExecutionMode.HEADLESS,
                                orchestrator_provider=Provider.CLAUDE))
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _drive_to(eng, Stage.SCOPE)
    assert "cannot enforce it" not in w.prompt
    task = eng.store.load_task("r1", "t1")
    assert w.prompt == render_prompt(
        Stage.SCOPE, task_id="t1", title=task.title, body=task.body,
        learnings="\n".join(task.learnings), context=task.context,
        project_commands=eng._project_commands(),
    )


def test_the_directive_names_only_the_posture_bits_that_are_denied() -> None:
    """Rendered from the policy, so a posture that keeps command execution never tells the
    model to stop running commands (SCOPE/REVIEW both scope and verify by running things)."""
    keeps_exec = render_prompt(
        Stage.REVIEW, task_id="t1", title="x", body="", tool_posture_unenforced=True
    )
    assert "Do NOT create, modify, or delete any file" in keeps_exec
    assert "Do NOT run shell commands" not in keeps_exec


def test_a_stage_with_no_posture_gets_no_directive_even_on_a_lane_that_cannot_enforce() -> None:
    """There is nothing to degrade for a stage that declares no posture — IMPLEMENT must not
    be told not to write."""
    prompt = render_prompt(
        Stage.IMPLEMENT, task_id="t1", title="x", body="", tool_posture_unenforced=True
    )
    assert "cannot enforce it" not in prompt
