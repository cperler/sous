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

from adapters.execution.review_panel import run_review_panel
from adapters.execution.transport import RawResult
from orchestrator.review_workflow import synthesize
from orchestrator.schemas.enums import ExecutionMode, Provider, Stage
from orchestrator.schemas.work import FinderSpec, LanePolicy, ReviewPlan, StageResult, WorkItem

_ROOT = Path(__file__).resolve().parent.parent
SHIM = _ROOT / "run_targets" / "workflow_shim.js"
DRIVER = Path(__file__).resolve().parent / "_shim_driver.mjs"


def _run_shim(mode: str) -> dict:
    proc = subprocess.run(  # noqa: S603
        [shutil.which("node"), str(DRIVER), str(SHIM), mode],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


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


def _panel_plan() -> ReviewPlan:
    return ReviewPlan(
        finders=(
            FinderSpec(lens="find:code", prompt="finder-code", agent="code-reviewer",
                       schema_ref="review_findings"),
            FinderSpec(lens="find:spec", prompt="finder-spec", agent="spec-reviewer",
                       schema_ref="review_findings"),
        ),
        verify_template="VERIFY {finding}\nAT {diff_hint}",
        verify_schema_ref="review_verdict",
        dedupe_rule="fingerprint-v1",
    )


class _PanelTransport:
    """Headless fake carrying the same script as the Node agent() harness."""

    def __init__(self) -> None:
        self.seen: list[WorkItem] = []

    def __call__(self, work: WorkItem) -> RawResult:
        self.seen.append(work)
        outputs = {
            "find:code": {"findings": [{
                "severity": "critical", "file": "a.py", "line": 7,
                "description": "Null deref in the guard",
            }]},
            "find:spec": {"findings": [
                {"severity": "critical", "file": "a.py", "line": 99,
                 "description": "null   DEREF in the guard"},
                {"severity": "suggestion", "file": "b.py", "line": 2,
                 "description": "rename this"},
            ]},
            "verify:1": {
                "fingerprint": "a.py:null deref in the guard",
                "verdict": "refuted",
                "reasoning": "guarded by the caller",
            },
        }
        return RawResult(outputs[work.phase], usage_recovered=False)


class _UnicodePanelTransport:
    """Headless script for the casefold and code-point truncation conformance vectors."""

    def __call__(self, work: WorkItem) -> RawResult:
        long_fingerprint = f":{'a' * 158}😀"
        outputs = {
            "find:code": {"findings": [
                {"severity": "critical", "file": "Straße.py", "line": 1,
                 "description": "BROKEN"},
                {"severity": "important", "file": "", "line": 2,
                 "description": f"{'a' * 158}😀discarded"},
                {"severity": "critical", "file": "\u1c89.py", "line": 3,
                 "description": "PINNED"},
            ]},
            "find:spec": {"findings": [
                {"severity": "critical", "file": "STRASSE.PY", "line": 99,
                 "description": "broken"},
                {"severity": "critical", "file": "\u1c8a.py", "line": 4,
                 "description": "pinned"},
            ]},
            "verify:1": {
                "fingerprint": "strasse.py:broken",
                "verdict": "confirmed",
                "reasoning": "confirmed across lanes",
            },
            "verify:2": {
                "fingerprint": "\u1c89.py:pinned",
                "verdict": "confirmed",
                "reasoning": "confirmed across lanes",
            },
            "verify:3": {
                "fingerprint": "\u1c8a.py:pinned",
                "verdict": "confirmed",
                "reasoning": "confirmed across lanes",
            },
            "verify:4": {
                "fingerprint": long_fingerprint,
                "verdict": "confirmed",
                "reasoning": "confirmed across lanes",
            },
        }
        return RawResult(outputs[work.phase], usage_recovered=False)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_plan_bearing_shim_conforms_to_the_headless_panel(monkeypatch) -> None:
    """Design §5(f): the real JS branch and Python reference return one equivalent panel.

    Lane attribution, completion time, and the WorkItem identity necessarily differ; every
    runner-owned panel field is compared, including unfolded evidence and per-call rows.
    """
    interactive = _run_shim("panel")
    actual = StageResult.model_validate(interactive["panelResult"])

    work = WorkItem.create(
        id="wi-headless", run_id="r", task_id="#1", stage=Stage.REVIEW,
        prompt="ordinary single-reviewer prompt", schema_ref="review",
        model="claude-opus-5", effort="high",
        lane_policy=LanePolicy(execution_mode=ExecutionMode.HEADLESS,
                               provider=Provider.CLAUDE),
        created_at="T", plan=_panel_plan(),
    )
    # Make required-but-unavailable interactive durations exactly comparable.
    monkeypatch.setattr("adapters.execution.review_panel.time.monotonic", lambda: 1.0)
    transport = _PanelTransport()
    expected = run_review_panel(work, transport)

    assert actual.status == expected.status
    assert actual.structured_output == expected.structured_output is None
    assert actual.raw_output == expected.raw_output
    assert actual.error == expected.error
    assert actual.sub_results == expected.sub_results
    assert actual.token_usage == expected.token_usage
    assert actual.schema_retries == expected.schema_retries == 0
    assert actual.usage_recovered == expected.usage_recovered is False
    assert [c.model_dump(mode="json") for c in actual.sub_calls or ()] == [
        c.model_dump(mode="json") for c in expected.sub_calls or ()
    ]
    assert [w.phase for w in transport.seen] == ["find:code", "find:spec", "verify:1"]

    calls = interactive["agentCallsDetailed"]
    assert all("$schema" not in keys for keys in interactive["schemaKeys"])
    assert [c["agentType"] for c in calls] == ["code-reviewer", "spec-reviewer", None]
    assert all(c["prompt"] != "ordinary single-reviewer prompt" for c in calls)
    assert "- line: 7" in calls[-1]["prompt"]  # fold-order representative, not duplicate
    assert calls[-1]["prompt"].endswith("AT a.py:7")  # mechanical diff-hint slot


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_plan_bearing_shim_matches_headless_unicode_fingerprints(monkeypatch) -> None:
    """Version-pinned casefolding and code-point truncation match on both lanes."""
    actual = StageResult.model_validate(_run_shim("panel-unicode")["panelResult"])
    work = WorkItem.create(
        id="wi-headless", run_id="r", task_id="#1", stage=Stage.REVIEW,
        prompt="ordinary single-reviewer prompt", schema_ref="review",
        model="claude-opus-5", effort="high",
        lane_policy=LanePolicy(execution_mode=ExecutionMode.HEADLESS,
                               provider=Provider.CLAUDE),
        created_at="T", plan=_panel_plan(),
    )
    monkeypatch.setattr("adapters.execution.review_panel.time.monotonic", lambda: 1.0)
    expected = run_review_panel(work, _UnicodePanelTransport())

    assert actual.sub_results == expected.sub_results
    assert [c.model_dump(mode="json") for c in actual.sub_calls or ()] == [
        c.model_dump(mode="json") for c in expected.sub_calls or ()
    ]
    # The sharp-s variants consume one verifier slot; U+1C89/U+1C8A remain two distinct
    # fingerprints under the pinned Unicode-15 contract even on runtimes that know their
    # newer case pairing; and the astral-boundary finding consumes the final slot.
    assert [c.phase for c in actual.sub_calls or ()] == [
        "find:code", "find:spec", "verify:1", "verify:2", "verify:3", "verify:4",
    ]
    assert [v["fingerprint"] for v in actual.sub_results["verdicts"]] == [
        "strasse.py:broken",
        "\u1c89.py:pinned",
        "\u1c8a.py:pinned",
        f":{'a' * 158}😀",
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_plan_bearing_shim_fails_on_a_missing_finder_and_not_an_inconclusive_verifier() -> None:
    finder_failure = StageResult.model_validate(_run_shim("panel-finder-error")["panelResult"])
    assert finder_failure.status.value == "failure"
    assert finder_failure.sub_results is None
    assert [c.phase for c in finder_failure.sub_calls or ()] == ["find:code", "find:spec"]
    assert "find:spec" in (finder_failure.error or "")

    verifier_failure = StageResult.model_validate(
        _run_shim("panel-verifier-error")["panelResult"]
    )
    assert verifier_failure.status.value == "success"
    assert verifier_failure.sub_results["verdicts"] == []
    assert verifier_failure.sub_results["notices"][0]["notice"] == "verifier_inconclusive"
    folded = synthesize(verifier_failure.sub_results)
    assert folded.review["approved"] is False and len(folded.review["issues"]) == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_plan_bearing_shim_caps_verifiers_without_dropping_findings() -> None:
    result = StageResult.model_validate(_run_shim("panel-cap")["panelResult"])
    assert len([c for c in result.sub_calls or () if c.phase.startswith("verify:")]) == 8
    cap = next(n for n in result.sub_results["notices"] if n["notice"] == "verifier_cap")
    assert cap["count"] == 4
    # Capped and inconclusive findings receive no refutation, so all remain blocking.
    assert len(synthesize(result.sub_results).review["issues"]) == 12
