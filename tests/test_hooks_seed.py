"""Safety + format hooks (audit gap 4): the guard fragments actually deny what they
guard (exercised against the real command strings, stdin-JSON interface), untagged
hooks reach every project, and seed_kit merges fragments into the project's
.claude/settings.json so they are LIVE — not inert examples."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from orchestrator.scaffold import (
    Profile,
    load_kit_manifest,
    seed_kit,
    select_kit_assets,
)

KIT_HOOKS = Path(__file__).resolve().parent.parent / "templates" / "project-default" / "hooks"


def _hook_command(name: str, event: str) -> str:
    frag = json.loads((KIT_HOOKS / f"{name}.json").read_text())
    return frag["hooks"][event][0]["hooks"][0]["command"]


def _run_hook(command: str, tool_input: dict) -> int:
    payload = json.dumps({"tool_input": tool_input})
    proc = subprocess.run(  # noqa: S603
        ["bash", "-c", command], input=payload, capture_output=True, text=True, timeout=10
    )
    return proc.returncode


def test_guard_sensitive_files_denies_and_allows() -> None:
    cmd = _hook_command("guard-sensitive-files", "PreToolUse")
    assert _run_hook(cmd, {"file_path": "/repo/.env"}) == 2  # denied
    assert _run_hook(cmd, {"file_path": "/repo/e2e-credentials.env"}) == 2
    assert _run_hook(cmd, {"file_path": "/repo/.git/config"}) == 2
    assert _run_hook(cmd, {"file_path": "/home/u/.ssh/id_rsa"}) == 2
    assert _run_hook(cmd, {"file_path": "/repo/src/app.py"}) == 0  # allowed
    assert _run_hook(cmd, {}) == 0  # no file_path -> allowed (fail-open)


def test_guard_deploy_denies_and_allows() -> None:
    cmd = _hook_command("guard-deploy", "PreToolUse")
    assert _run_hook(cmd, {"command": "terraform apply -auto-approve"}) == 2
    assert _run_hook(cmd, {"command": "npx cdk deploy --all"}) == 2
    assert _run_hook(cmd, {"command": "./scripts/deploy_to_production.sh"}) == 2
    assert _run_hook(cmd, {"command": "ls -la && git status"}) == 0
    assert _run_hook(cmd, {"command": "terraform plan"}) == 0  # plan is read-only


def test_format_hooks_use_stdin_json_not_env_var() -> None:
    for name in ("python-format", "typescript-format"):
        cmd = _hook_command(name, "PostToolUse")
        assert "tool_input.file_path" in cmd  # the proven stdin-JSON interface
        assert "$CLAUDE_FILE_PATH" not in cmd  # the unverified env-var form is gone


def test_untagged_safety_hooks_selected_for_every_stack() -> None:
    manifest = load_kit_manifest()
    for langs in ([], ["python"], ["typescript"], ["go"]):
        hooks = select_kit_assets(Profile(name="p", languages=langs), manifest)["hooks"]
        assert "guard-sensitive-files" in hooks and "guard-deploy" in hooks
    py = select_kit_assets(Profile(name="p", languages=["python"]), manifest)["hooks"]
    assert "python-format" in py and "typescript-format" not in py


def test_seed_kit_merges_hooks_into_settings(tmp_path) -> None:
    assets = {"hooks": ["guard-sensitive-files", "guard-deploy", "python-format"]}
    # Pre-existing settings must be preserved, never clobbered.
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))

    seeded = seed_kit(assets, tmp_path)
    assert "settings.json (hooks merged)" in seeded

    settings = json.loads((claude / "settings.json").read_text())
    assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}  # untouched
    pre = settings["hooks"]["PreToolUse"]
    assert len(pre) == 2  # both guards live
    assert len(settings["hooks"]["PostToolUse"]) == 1  # the format hook

    # Idempotent: re-seeding adds nothing.
    seed_kit(assets, tmp_path)
    again = json.loads((claude / "settings.json").read_text())
    assert again == settings


def test_seed_kit_never_clobbers_broken_settings(tmp_path) -> None:
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text("{not json")
    seed_kit({"hooks": ["guard-deploy"]}, tmp_path)
    assert (claude / "settings.json").read_text() == "{not json"  # left alone
