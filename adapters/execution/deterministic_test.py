"""Deterministic TEST runner — the ENGINE lane (no model call).

#33: running the project's test commands and reporting
pass/fail is mechanical. A model only adds cost, flakiness, and the risk of MISreporting
a red suite as green. This runner shells the project's declared test commands in the task
worktree, bounds their output, classifies failures via the project's ``FailureClassifier``,
and returns the ``test`` stage contract deterministically at $0.

MEANINGFULNESS IS DELIBERATELY NOT JUDGED HERE. The ``test`` schema's ``tests_meaningful``
asks whether the tests genuinely exercise the change — a judgment a script cannot make.
So this runner emits ``tests_meaningful: null`` on every exit path (#261): null is the
honest "no claim" encoding — it is fail-OPEN everywhere (every gate tests ``is False``),
it keeps the key present so the schema's ``required`` list still catches a codex model that
simply omits it, and — unlike the ``true`` this used to write — it does not put an
affirmative verification into the audit record that nobody performed. It never emits
``false`` either (that is the engine's veto in ``Engine._stage_gate``, which would reject
the stage over a judgment this runner did not make). The real meaningfulness call belongs to
the model REVIEW / test-validate path, so a pipeline that runs deterministic TEST MUST keep
a model REVIEW stage (micro/lite do) — and when NEITHER side judges it, the engine emits a
warning-grade ``test_validation_skipped`` event rather than passing quietly (#261).

Baseline handling mirrors the TEST stage template (orchestrator/stages.py): a failing test
already present in intake's ``baseline_failures`` was RED at base — inherited, not caused —
so it is excluded from the caused-failure verdict and only noted. A nonzero exit with
caused failures fails the stage with a classified error so the engine's retry / infra-reset
loops engage; a green (or only-inherited) run succeeds.

Impacted-only runs (#558): when the project adapter declares the optional
``impacted_tests`` hook, the UNIT command runs only the tests it maps from the task's diff
(``test_unit_cmd(files)``); e2e/shell always run whole. Every fallback — hook missing,
raising, returning None, a global test input touched, an empty set — runs the full suite
(``orchestrator.test_selection``), and the result records ``test_scope`` (impacted|full),
``tests_selected`` and ``test_scope_reason`` so a green subset never reads as a full green.

Flaky failures: when the UNIT command fails on a few named tests, those tests are re-run
once — narrowed via ``test_unit_cmd(failed)`` when the adapter honors it, else the whole
unit command again. A re-run that passes means the failures did not reproduce: they are
recorded under ``flaky_failures`` and a warning notice, not counted as caused. Prediction
run ``batch-418-419-377-386-388`` failed a task three times on three different
timing-sensitive tests its change never touched; one failing run is not proof.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from orchestrator.failure_classifier import Failure
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
from orchestrator.schemas.work import StageResult, WorkItem
from orchestrator.test_selection import TestSelection, changed_files, select_tests

from .transport import RawResult, _tag_head, subprocess_env, to_stage_result
from .worktree_origin import verify_worktree_origin

_MAX_TAIL = 4000  # bounded output tail kept in validation_notes (per command)
# More caused unit failures than this is treated as a real break, not flakiness — no re-run.
_MAX_FLAKY_RERUN = 5

# Documentation-file classification for the #41 docs-only change tag. Deliberately
# conservative: a change is "docs-only" only when EVERY changed file is unambiguously
# documentation, so a non-doc file can never be mislabeled away from a real test run.
_DOC_SUFFIXES = {".md", ".markdown", ".rst", ".txt", ".adoc"}
_DOC_STEMS = {"license", "notice", "authors", "changelog", "contributing", "readme"}


def _is_doc_file(path: str) -> bool:
    p = PurePosixPath(path.strip())
    if not p.name:
        return False
    if "docs" in p.parts:  # anything under a docs/ directory
        return True
    if p.suffix.lower() in _DOC_SUFFIXES:
        return True
    # Extensionless doc files (LICENSE, NOTICE, …). Only when there is no suffix, so a
    # source file that merely shares the stem (license.py) is NOT treated as docs.
    return not p.suffix and p.stem.lower() in _DOC_STEMS


# The git read moved engine-side with impacted-test selection (#558) so the model TEST
# lane's prompt and this runner share one implementation; the private alias keeps the
# name this module (and its tests) always used.
_changed_files = changed_files


def classify_change(cwd: str | None, base_sha: str | None) -> str | None:
    """Deterministically classify the task's diff as ``"docs-only"`` or ``"code"`` — or
    None when it can't be determined (missing cwd/base, git error, or an empty diff). Only
    a real git read sets this, so a model can never claim docs-only (the #41 loophole guard
    lives at the fold, but detection itself is git-only by construction). ``"docs-only"``
    requires a NON-EMPTY changeset where every file is documentation; a single non-doc file
    yields ``"code"``."""
    if not cwd or not base_sha:
        return None
    return _class_of(_changed_files(cwd, base_sha))


def _class_of(files: list[str] | None) -> str | None:
    """``classify_change`` over an already-read changed-file list."""
    if not files:  # None (git error) or [] (nothing changed) → undetermined
        return None
    return "docs-only" if all(_is_doc_file(f) for f in files) else "code"


@dataclass(frozen=True)
class TestRun:
    """One evaluation of the declared test commands (see ``DeterministicTestRunner.evaluate``)."""

    __test__ = False  # not a pytest class, despite the name

    out: dict | None
    error: str | None
    ok: bool
    notices: tuple[dict[str, object], ...] = ()
    refused: bool = False


class DeterministicTestRunner:
    """In-process ENGINE-lane runner for the TEST stage (delegated from the ENGINE cell)."""

    def __init__(self, project: object, *, timeout_s: int = 900) -> None:
        self._project = project  # ProjectConfig: test_*_cmd surfaces + optional classifier
        self._timeout_s = timeout_s

    def dispatch(self, work: WorkItem) -> StageResult:
        run = self.evaluate(work)
        # A TEST stage is checkpointed (StageSpec.checkpoint) — anchor HEAD on success so a
        # later stage's reset has an anchor even when the deterministic run made no commit.
        checkpoint = (
            _tag_head(work.cwd, work.checkpoint_tag)
            if run.ok and work.checkpoint_tag and work.cwd else None
        )
        status = ResultStatus.SUCCESS if run.ok else ResultStatus.FAILURE
        raw = RawResult(
            run.out, exit_code=0 if run.ok else 1, error=run.error,
            invocation="engine:test", checkpoint=checkpoint,
            execution_notices=run.notices,
        )
        return to_stage_result(work, raw, status,
                               mode=ExecutionMode.ENGINE, provider=Provider.NONE)

    def evaluate(self, work: WorkItem) -> TestRun:
        """Run the project's declared test commands over ``work.cwd`` and return the verdict
        without building a StageResult or touching git refs. Shared by :meth:`dispatch` and
        by the model lanes' verdict check (``test_verdict``), so a model TEST stage and the
        ENGINE lane judge pass/fail with the same commands and the same baseline rules.
        ``refused`` marks a verdict that never ran a command (no worktree, untrusted
        toolchain origin, a raising run) — distinct from a suite that ran and went red."""
        if not work.cwd:
            return TestRun(None, "test worktree is unspecified; refusing to run verification "
                                 "commands", False, refused=True)
        origin = verify_worktree_origin(self._project, Path(work.cwd))
        if not origin.trusted:
            return TestRun(None, "worktree toolchain origin mismatch; refusing to trust test "
                                 "results", False, notices=origin.notices, refused=True)
        try:
            out, error, ok = self._run_tests(work)
        except Exception as exc:  # noqa: BLE001 - every dispatch MUST yield a StageResult,
            # never an escaped exception (which would leave the dispatch lease held with no
            # path to clear it). Mirror deterministic_setup's convert-to-FAILURE contract.
            return TestRun(None, str(exc), False, notices=origin.notices, refused=True)
        notices = origin.notices
        if flaky := out.get("flaky_failures"):
            notice: dict[str, object] = {
                "notice": "test_flaky_failures_cleared",
                "reason": "passed_on_rerun",
                "failures": list(flaky)[:10],
                "detail": "unit test failure(s) did not reproduce when re-run once; not "
                          "counted against the change",
            }
            notices = notices + (notice,)
        return TestRun(out, error, ok, notices=notices)

    def _run_tests(self, work: WorkItem) -> tuple[dict, str | None, bool]:
        baseline = set(_as_ids((work.context or {}).get("baseline_failures")))
        caused: list[str] = []
        caused_kinds: set[str] = set()
        inherited_count = 0
        flaky: list[str] = []
        notes: list[str] = []
        # #5: export the task's per-task port block into the test subprocess so a suite that
        # boots a dev/test server binds THIS task's ports, never a sibling worktree's.
        proc_env = subprocess_env(work)

        # #41: classify the change deterministically (git diff of base_sha..worktree). A
        # docs-only change has no behavioral surface, so a full test run is wasted effort —
        # short-circuit to an HONEST skip (marked skipped: docs-only, never a faked green).
        # The tag also flows into the context plane (ENGINE-lane-only fold) so REVIEW skips
        # test-coverage criteria and the engine won't reject it for lacking new tests.
        base_sha = (work.context or {}).get("base_sha")
        changed = _changed_files(work.cwd, base_sha) if work.cwd and base_sha else None
        change_class = _class_of(changed)
        if change_class == "docs-only":
            out = {
                "passed": True,
                "failures": [],
                "tests_meaningful": None,  # not judged here — see module docstring / REVIEW
                "change_class": "docs-only",
                "skipped": "docs-only",
                "validation_notes": "docs-only change (every changed file is documentation): "
                                    "no behavioral surface to test — skipped the suite. "
                                    "Classified deterministically by diffing base_sha..worktree "
                                    "(no test run faked green).",
            }
            return out, None, True

        # For a non-docs-only change, surface the classification too (informative in the
        # log; "code" is the normal path — it changes no downstream behavior).
        extra = {"change_class": change_class} if change_class else {}

        # #558: narrow the UNIT command to the tests the adapter maps from this diff, or run
        # the full suite on every fallback. Resolved after the docs-only skip, so an empty
        # impacted set here always means "no mapping" (→ full), never "nothing to test".
        commands, selection = self._commands(select_tests(self._project, changed, cwd=work.cwd))

        if not commands:
            out = {
                "passed": True,
                "failures": [],
                "tests_meaningful": None,  # not judged here — see module docstring / REVIEW
                "validation_notes": "deterministic test run: no test commands defined "
                                    "(nothing to run); meaningfulness NOT judged here "
                                    "(reported as null) — the model REVIEW path judges it.",
                **extra,
            }
            return out, None, True

        scope = {
            "test_scope": selection.scope,
            "tests_selected": len(selection.files),
            "test_scope_reason": selection.reason,
        }
        if selection.scope == "impacted":
            notes.append(f"unit: ran {len(selection.files)} impacted test file(s), NOT the "
                         f"full suite ({selection.reason})")
        else:
            notes.append(f"unit: full suite ({selection.reason})")

        for label, argv in commands:
            try:
                proc = subprocess.run(  # noqa: S603
                    argv, cwd=work.cwd, capture_output=True, text=True,
                    timeout=self._timeout_s, env=proc_env,
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
            if new and label == "unit" and len(new) <= _MAX_FLAKY_RERUN:
                ids = [f.test for f in new]
                cleared, how = self._rerun_cleared(argv, ids, work.cwd, proc_env)
                if cleared:
                    flaky.extend(ids)
                    notes.append(f"{label}: rc={proc.returncode}, but {len(ids)} failure(s) "
                                 f"passed when {how} was re-run once — flaky, not counted: "
                                 + ", ".join(ids))
                    continue
                notes.append(f"{label}: re-ran {how} once; still red")
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
        notes.append("tests_meaningful not judged by this runner (reported as null) — the "
                     "model REVIEW path verifies the tests genuinely exercise the change.")
        passed = not caused
        out = {
            "passed": passed,
            "failures": caused[:40],
            "tests_meaningful": None,  # see module docstring: no claim, never a fake true
            "validation_notes": "; ".join(notes),
            **extra,
            **scope,
            **({"flaky_failures": flaky} if flaky else {}),
        }
        error = None if passed else (
            f"test stage red: {len(caused)} caused failure(s) "
            f"(kinds: {', '.join(sorted(caused_kinds)) or 'unknown'})"
        )
        return out, error, passed

    def _rerun_cleared(
        self, argv: list[str], failed: list[str], cwd: str | None, env: dict[str, str] | None,
    ) -> tuple[bool, str]:
        """Re-run once the unit tests that just failed; True when that run is green. Uses
        ``test_unit_cmd(failed)`` when the adapter narrows on it, else ``argv`` again."""
        unit = getattr(self._project, "test_unit_cmd", None)
        narrowed = _argv_of(unit, failed)
        if narrowed and narrowed != _argv_of(unit):
            rerun, how = narrowed, "the failed test(s)"
        else:
            rerun, how = argv, "the unit command"
        try:
            proc = subprocess.run(  # noqa: S603
                rerun, cwd=cwd, capture_output=True, text=True,
                timeout=self._timeout_s, env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return False, how
        return proc.returncode == 0, how

    def _commands(
        self, selection: TestSelection
    ) -> tuple[list[tuple[str, list[str]]], TestSelection]:
        """The project's runnable test commands, skipping the ``['true']`` no-op sentinel
        and undefined/empty surfaces (a project without e2e/shell simply omits them —
        mirrors ``engine._project_commands`` and ``deterministic_setup``).

        An ``impacted`` selection narrows ONLY the unit command, via
        ``test_unit_cmd(files)``. If that call raises, yields no runnable argv, or ignores
        ``files`` (same argv as the no-arg call), the unit command falls back to the full suite and the returned selection says so — the
        recorded scope is always the scope that actually ran."""
        out: list[tuple[str, list[str]]] = []
        unit = getattr(self._project, "test_unit_cmd", None)
        if selection.scope == "impacted":
            argv = _argv_of(unit, list(selection.files))
            if argv and argv != _argv_of(unit):
                out.append(("unit", argv))
            else:
                # No runnable argv, or the adapter ignored ``files`` and returned its
                # full-suite argv — either way the full suite is what runs.
                selection = TestSelection(
                    scope="full", files=(),
                    reason="test_unit_cmd(files) gave no narrowed command for the "
                           "impacted set",
                )
        if selection.scope == "full" and (argv := _argv_of(unit)):
            out.append(("unit", argv))
        for label, getter in (
            ("e2e", getattr(self._project, "test_e2e_cmd", None)),
            ("shell", getattr(self._project, "test_shell_cmd", None)),
        ):
            argv = _argv_of(getter)
            if argv:
                out.append((label, argv))
        return out, selection

    def _classify(self, test_output: str) -> list[Failure]:
        """Structured failures from raw output via the project classifier (best-effort)."""
        classifier = getattr(self._project, "classifier", None)
        if classifier is None:
            return []
        try:
            return [f for f in classifier.classify(test_output) if f.test != "<infra>"]
        except Exception:  # noqa: BLE001 - classification must never crash the stage
            return []


def _argv_of(getter: object, files: list[str] | None = None) -> list[str] | None:
    if not callable(getter):
        return None
    try:
        argv = getter(files) if files else getter()
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
