"""Behavioral regression for the JS workflow shim (run_targets/workflow_shim.js).

Locks in two runtime-hardening fixes surfaced by the first live interactive×claude
run (#30): the Workflow runtime can deliver `args` as a JSON *string* (not an object),
and the engine's canonical stage schemas carry a top-level `$schema`/`$id` that agent()'s
validator rejects. Driven through node (skipped when node is unavailable) so the assertions
exercise the actual shim, not a Python mirror.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
SHIM = _ROOT / "run_targets" / "workflow_shim.js"
DRIVER = Path(__file__).resolve().parent / "_shim_driver.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_shim_parses_string_args_and_sanitizes_schema() -> None:
    proc = subprocess.run(  # noqa: S603
        [shutil.which("node"), str(DRIVER), str(SHIM)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(proc.stdout)

    # (1) args-as-string was parsed: both work items dispatched and succeeded
    # (a string that was NOT parsed would yield `items = []` -> zero results).
    assert data["resultCount"] == 2
    assert data["statuses"] == ["success", "success"]
    # `now` from the (string) args flows onto the result — proves the parse, not a default.
    assert data["completedAt"] == ["T", "T"]

    # (2) the schema handed to agent() had its meta-keys stripped (else agent() errors
    # with `no schema with key or ref "https://json-schema.org/..."`).
    assert data["schemaKeys"], "agent() received no schema"
    for keys in data["schemaKeys"]:
        assert keys is not None
        assert "$schema" not in keys and "$id" not in keys
        assert "type" in keys and "properties" in keys  # real schema body preserved

    # (3) #96 effort pass-through: wi-1 (effort="high") reaches agent() as the `effort`
    # opt and is echoed on its result row; effort-less wi-2 stays effort-free on both
    # sides (undefined -> absent in the agent opts JSON, null on the result row).
    assert data["agentEffort"] == {"p1": "high"}  # p2 absent: no effort forwarded
    assert data["resultEffort"] == {"wi-1": "high", "wi-2": None}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_shim_refuses_a_malformed_content_hash_before_dispatching() -> None:
    """#311: a truncated content_hash aborts the batch with ZERO model spend.

    The live failure was a 16-char log preview pasted over the 64-char digest, echoed back
    onto both StageResults. record() refuses such a result — but only after the stage has
    been paid for. The shim checks the shape first, so the transcription slip costs
    nothing and the message tells the supervisor how to re-dispatch."""
    proc = subprocess.run(  # noqa: S603
        [shutil.which("node"), str(DRIVER), str(SHIM), "badhash"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(proc.stdout)

    assert "content_hash" in data["threw"] and "64-char" in data["threw"]
    assert "wi-2" in data["threw"]  # names the offending item, not just "a work item"
    # The whole batch is refused: the VALID sibling was not dispatched either, so a
    # partial batch can never land results the supervisor thinks it re-ran in full.
    assert data["agentCalls"] == 0
