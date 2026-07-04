"""Deterministic TEST runner — the ENGINE lane (no model call).

heysoo #227 follow-up (#33): running the project's test commands and reporting
pass/fail is mechanical. A model only adds cost, flakiness, and the risk of MISreporting
a red suite as green. This runner shells the project's declared test commands in the task
worktree, bounds their output, classifies failures via the project's ``FailureClassifier``,
and returns the ``test`` stage contract deterministically at $0.

MEANINGFULNESS IS DELIBERATELY NOT JUDGED HERE. The ``test`` schema's ``tests_meaningful``
asks whether the tests genuinely exercise the change — a judgment a script cannot make.
So this runner NEVER emits ``tests_meaningful: false`` (that is the engine's veto in
``Engine._stage_gate``, which would reject the stage): it emits ``true`` — meaning only
"this runner did not flag the tests as vacuous" — and leaves the real meaningfulness call
to the model REVIEW / test-validate path, which independently re-reports it. A pipeline
that runs deterministic TEST MUST therefore keep a model REVIEW stage (micro/lite do).

Baseline handling mirrors the TEST stage template (orchestrator/stages.py): a failing test
already present in intake's ``baseline_failures`` was RED at base — inherited, not caused —
so it is excluded from the caused-failure verdict and only noted. A nonzero exit with
caused failures fails the stage with a classified error so the engine's retry / infra-reset
loops engage; a green (or only-inherited) run succeeds.
"""

from __future__ import annotations

import subprocess

from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
from orchestrator.schemas.work import StageResult, WorkItem

from .transport import RawResult, _tag_head, to_stage_result

_MAX_TAIL = 4000  # bounded output tail kept in validation_notes (per command)


class DeterministicTestRunner:
    """In-process ENGINE-lane runner for the TEST stage (delegated from the ENGINE cell)."""

    def __init__(self, project: object, *, timeout_s: int = 900) -> None:
        self._project = project  # ProjectConfig: test_*_cmd surfaces + optional classifier
        self._timeout_s = timeout_s

    def dispatch(self, work: WorkItem) -> StageResult:
        try:
            out, error, ok = self._run_tests(work)
        except Exception as exc:  # noqa: BLE001 - every dispatch MUST yield a StageResult,
            # never an escaped exception (which would leave the dispatch lease held with no
            # path to clear it). Mirror deterministic_setup's convert-to-FAILURE contract.
            raw = RawResult(None, exit_code=1, error=str(exc), invocation="engine:test")
            return to_stage_result(work, raw, ResultStatus.FAILURE,
                                   mode=ExecutionMode.ENGINE, provider=Provider.NONE)
        # A TEST stage is checkpointed (StageSpec.checkpoint) — anchor HEAD on success so a
        # later stage's reset has an anchor even when the deterministic run made no commit.
        checkpoint = (
            _tag_head(work.cwd, work.checkpoint_tag)
            if ok and work.checkpoint_tag and work.cwd else None
        )
        status = ResultStatus.SUCCESS if ok else ResultStatus.FAILURE
        raw = RawResult(out, exit_code=0 if ok else 1, error=error,
                        invocation="engine:test", checkpoint=checkpoint)
        return to_stage_result(work, raw, status,
                               mode=ExecutionMode.ENGINE, provider=Provider.NONE)

    def _run_tests(self, work: WorkItem) -> tuple[dict, str | None, bool]:
        baseline = set(_as_ids((work.context or {}).get("baseline_failures")))
        commands = self._commands()
        caused: list[str] = []
        caused_kinds: set[str] = set()
        inherited_count = 0
        notes: list[str] = []

        if not commands:
            out = {
                "passed": True,
                "failures": [],
                "tests_meaningful": True,  # not judged here — see module docstring / REVIEW
                "validation_notes": "deterministic test run: no test commands defined "
                                    "(nothing to run); meaningfulness judged by REVIEW.",
            }
            return out, None, True

        for label, argv in commands:
            try:
                proc = subprocess.run(  # noqa: S603
                    argv, cwd=work.cwd, capture_output=True, text=True, timeout=self._timeout_s
                )
            except subprocess.TimeoutExpired:
                caused.append(f"<{label} timeout>")
                caused_kinds.add("infra")
                notes.append(f"{label}: TIMED OUT after {self._timeout_s}s")
                continue
            except (OSError, subprocess.SubprocessError) as exc:
                caused.append(f"<{label} {type(exc).__name__}>")
                caused_kinds.add("infra")
                notes.append(f"{label}: run error ({type(exc).__name__})")
                continue
            if proc.returncode == 0:
                notes.append(f"{label}: green")
                continue
            combined = f"{proc.stdout}\n{proc.stderr}"
            failures = self._classify(combined)
            new = [f for f in failures if f.test not in baseline]
            inherited = [f for f in failures if f.test in baseline]
            inherited_count += len(inherited)
            if new:
                caused.extend(f.test for f in new)
                caused_kinds.update(f.kind for f in new)
                notes.append(f"{label}: FAILED rc={proc.returncode} — {len(new)} caused"
                             + (f", {len(inherited)} inherited (excluded)" if inherited else ""))
            elif inherited:
                # Every failure was already red at base — inherited, not caused by this change.
                notes.append(f"{label}: rc={proc.returncode} but all {len(inherited)} failures "
                             f"are inherited baseline red (excluded, not counted)")
            else:
                # Nonzero exit the classifier couldn't parse — honest: treat as a caused red
                # so the engine retries rather than silently shipping an unexplained failure.
                caused.append(f"<{label} rc={proc.returncode}>")
                caused_kinds.add("unknown")
                notes.append(f"{label}: FAILED rc={proc.returncode}; failures unparsed")
                notes.append(f"{label} output tail: {combined.strip()[-_MAX_TAIL:]}")

        if inherited_count:
            notes.append(f"{inherited_count} inherited baseline failure(s) excluded "
                         "(RED at base — not this change's regression).")
        notes.append("tests_meaningful not judged by this runner — the model REVIEW path "
                     "verifies the tests genuinely exercise the change.")
        passed = not caused
        out = {
            "passed": passed,
            "failures": caused[:40],
            "tests_meaningful": True,  # see module docstring: never false from a script
            "validation_notes": "; ".join(notes),
        }
        error = None if passed else (
            f"test stage red: {len(caused)} caused failure(s) "
            f"(kinds: {', '.join(sorted(caused_kinds)) or 'unknown'})"
        )
        return out, error, passed

    def _commands(self) -> list[tuple[str, list[str]]]:
        """The project's runnable test commands, skipping the ``['true']`` no-op sentinel
        and undefined/empty surfaces (a project without e2e/shell simply omits them —
        mirrors ``engine._project_commands`` and ``deterministic_setup``)."""
        out: list[tuple[str, list[str]]] = []
        for label, getter in (
            ("unit", getattr(self._project, "test_unit_cmd", None)),
            ("e2e", getattr(self._project, "test_e2e_cmd", None)),
            ("shell", getattr(self._project, "test_shell_cmd", None)),
        ):
            argv = _argv_of(getter)
            if argv:
                out.append((label, argv))
        return out

    def _classify(self, test_output: str):
        """Structured failures from raw output via the project classifier (best-effort)."""
        classifier = getattr(self._project, "classifier", None)
        if classifier is None:
            return []
        try:
            return [f for f in classifier.classify(test_output) if f.test != "<infra>"]
        except Exception:  # noqa: BLE001 - classification must never crash the stage
            return []


def _argv_of(getter: object) -> list[str] | None:
    if not callable(getter):
        return None
    try:
        argv = getter()
    except Exception:  # noqa: BLE001 - a project command surface must never crash the stage
        return None
    if not argv or argv == ["true"]:  # the no-op sentinel means "skip"
        return None
    return list(argv)


def _as_ids(value: object) -> list[str]:
    """Baseline_failures as a list of test-id strings (tolerant of None / non-list)."""
    if isinstance(value, list):
        return [str(x) for x in value]
    return []
