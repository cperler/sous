"""#375: the codex fresh+write-capable argv must use flags the installed CLI still accepts.

codex-cli 0.147.0 REMOVED `--full-auto`, so every dispatch emitting it died instantly with
`error: unexpected argument '--full-auto' found` — exit 2, before the model ever ran.

Why it stayed latent through a whole batch: that branch is reached only by a COLD start on a
WRITING stage, and no ordinary task order produces one. SCOPE cold-starts but is write-denying,
so it takes `--sandbox read-only`; it then seeds the `session_ref` that IMPLEMENT/TEST/DELIVER
all resume, and the resume shape sets its posture via `-c` overrides. On `batch-369-371` the
branch was finally hit when REVIEW rejected #370 into a fix cycle and the re-dispatched
IMPLEMENT arrived with no session — two attempts, both failing in the same second, breaker
tripped, task lost after DELIVER had already pushed its PR.

Two guards, because they fail for different reasons:

* the shape test pins what we emit — workspace-write plus non-blocking approvals, the pair
  `--full-auto` stood for — and fails if someone reintroduces the removed flag;
* the parser test asks the REAL installed CLI whether it still documents every long flag we
  emit, which is the only thing that would have caught 0.147.0's removal at upgrade time
  rather than mid-run. Skipped when codex is not installed, so CI stays hermetic.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from adapters.execution.transport import codex_cli_transport
from orchestrator.schemas.enums import ExecutionMode, Provider, Stage
from orchestrator.schemas.work import LanePolicy, ToolPolicy, WorkItem

H = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)
READ_ONLY = ToolPolicy(allow_file_writes=False)


def _stub_run(calls: list):
    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"result": "ok"}), stderr="")

    return fake_run


def _work(**kw) -> WorkItem:
    args: dict = dict(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.IMPLEMENT, prompt="make the change",
        schema_ref="implement", model="gpt-5.5", created_at="now", lane_policy=H, cwd=None,
    )
    args.update(kw)
    return WorkItem.create(**args)


def _argv_for(**kw) -> list[str]:
    calls: list = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subprocess, "run", _stub_run(calls))
        codex_cli_transport()(_work(**kw))
    return calls[0]


def test_cold_start_writing_stage_asks_for_workspace_write() -> None:
    """The #370 fix-cycle shape: IMPLEMENT dispatched with no warm session still has to be
    able to write, and must say so with a flag that exists."""
    argv = _argv_for()
    assert "--full-auto" not in argv, "codex-cli 0.147.0 removed this flag; it exits 2"
    assert argv[:2] == ["codex", "exec"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    # Approvals stay non-blocking: an unattended batch has nobody to answer a prompt, so a
    # denial must fail the command rather than stall it.
    assert 'approval_policy="never"' in argv


def test_cold_start_read_only_stage_is_unchanged() -> None:
    """The write-denying posture keeps its own containment — this branch was never broken and
    must not be collapsed into the workspace-write one while fixing it."""
    argv = _argv_for(stage=Stage.REVIEW, schema_ref="review", tool_policy=READ_ONLY)
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_resume_shape_still_carries_its_posture_via_config() -> None:
    """`codex exec resume` takes no sandbox flag, so the posture rides on `-c` overrides. Pinned
    here so the fix above cannot be "helpfully" applied to a subcommand that would reject it."""
    argv = _argv_for(session_ref="thread-1")
    assert argv[:4] == ["codex", "exec", "resume", "thread-1"]
    assert "--sandbox" not in argv
    assert 'sandbox_mode="workspace-write"' in argv


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI not installed")
@pytest.mark.parametrize(
    "kw",
    [
        pytest.param({}, id="cold-start-write"),
        pytest.param({"stage": Stage.REVIEW, "schema_ref": "review", "tool_policy": READ_ONLY},
                     id="cold-start-read-only"),
        pytest.param({"session_ref": "thread-1"}, id="resume"),
    ],
)
def test_every_long_flag_is_still_documented_by_the_installed_cli(kw) -> None:
    """The guard that would have caught 0.147.0 at upgrade time instead of mid-run.

    Asks the real binary's help for each long option we emit. Cheap, no dispatch, no network —
    and it fails the moment an upgrade retires a flag we depend on."""
    sub = ["exec"] if "session_ref" not in kw else ["exec", "resume"]
    help_text = subprocess.run(  # noqa: S603
        ["codex", *sub, "--help"], capture_output=True, text=True, timeout=60
    ).stdout

    argv = _argv_for(**kw)
    emitted = {a for a in argv if a.startswith("--")}
    missing = sorted(f for f in emitted if f not in help_text)
    assert not missing, f"installed codex no longer accepts: {missing}"
