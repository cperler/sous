"""#351 (second half): the codex lane's sandbox must let a stage reach the network.

The first half of #351 made a DELIVER that opened no PR a stage FAILURE
(`state_machine.pr_not_opened`). This is the half that stops the failure from happening:
every DELIVER on `batch-codex-3` reported "GitHub API unreachable" because codex's
`workspace-write` sandbox denies network egress by default — `gh`, `curl` and `git` over
https all fail to resolve a host inside it, while the same commands with the same auth work
from an ordinary shell. Reproduced on codex-cli 0.146.0; the one config override asserted
here flips codex's own banner to `(network access enabled)` and all three succeed.

The load-bearing properties:

* BOTH workspace-write call shapes carry the grant — the fresh call (`--full-auto` when this
  was written; `--sandbox workspace-write` since #375 removed the flag) and the `resume` call. Continuity must not silently revert it, which is exactly how #272's posture
  bug worked and why the resume shape is tested separately rather than assumed;
* the read-only sandbox does NOT get it: the key is namespaced under `sandbox_workspace_write`
  and codex exposes no network knob for `--sandbox read-only`, so emitting it there would be
  inert noise contradicting a posture that asked for containment;
* the claude lane is untouched — it has no sandbox and never took this flag.
"""

from __future__ import annotations

import json
import subprocess

from adapters.execution.transport import claude_cli_transport, codex_cli_transport
from orchestrator.schemas.enums import ExecutionMode, Provider, Stage
from orchestrator.schemas.work import LanePolicy, ToolPolicy, WorkItem

NETWORK_CFG = "sandbox_workspace_write.network_access=true"

H = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)
READ_ONLY = ToolPolicy(allow_file_writes=False)


def _stub_run(calls: list):
    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"result": "ok"}), stderr="")

    return fake_run


def _work(**kw) -> WorkItem:
    args: dict = dict(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.DELIVER, prompt="open the PR",
        schema_ref="deliver", model="gpt-5.5", created_at="now", lane_policy=H, cwd=None,
    )
    args.update(kw)
    return WorkItem.create(**args)


def test_fresh_workspace_write_call_grants_network(monkeypatch) -> None:
    """The DELIVER shape from `batch-codex-3`: without this, `gh pr create` cannot resolve
    api.github.com and the stage reports a compare link instead of a pull request."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    codex_cli_transport()(_work())
    argv = calls[0]
    # #375: the fresh workspace-write shape is `--sandbox workspace-write`, not the
    # `--full-auto` codex-cli 0.147.0 removed.
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert NETWORK_CFG in argv
    # ...and it is a `-c` config override, not a bare argument the CLI would reject.
    assert argv[argv.index(NETWORK_CFG) - 1] == "-c"


def test_resume_call_grants_network(monkeypatch) -> None:
    """Session continuity is where the grant would silently disappear — the DELIVER that
    failed on `batch-codex-3` was a `codex exec resume`, not a fresh call."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    codex_cli_transport()(_work(session_ref="thread-1"))
    argv = calls[0]
    assert argv[:4] == ["codex", "exec", "resume", "thread-1"]
    assert 'sandbox_mode="workspace-write"' in argv
    assert NETWORK_CFG in argv
    assert argv[argv.index(NETWORK_CFG) - 1] == "-c"


def test_read_only_sandbox_does_not_claim_network(monkeypatch) -> None:
    """A write-denying posture keeps the containment it asked for. The key only exists for
    workspace-write, so emitting it under `--sandbox read-only` would grant nothing while
    reading as a grant."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    codex_cli_transport()(_work(stage=Stage.REVIEW, schema_ref="review", tool_policy=READ_ONLY))
    assert calls[0][calls[0].index("--sandbox") + 1] == "read-only"
    assert NETWORK_CFG not in calls[0]

    calls.clear()
    codex_cli_transport()(
        _work(stage=Stage.REVIEW, schema_ref="review", tool_policy=READ_ONLY,
              session_ref="thread-1")
    )
    assert 'sandbox_mode="read-only"' in calls[0]
    assert NETWORK_CFG not in calls[0]


def test_claude_lane_is_untouched(monkeypatch) -> None:
    """The sandbox is codex's primitive; the claude lane never had one to widen."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    claude_cli_transport()(_work(model="claude-opus-5"))
    assert not any(NETWORK_CFG in str(a) for a in calls[0])
