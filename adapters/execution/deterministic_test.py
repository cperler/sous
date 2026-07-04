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
from pathlib import PurePosixPath

from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
from orchestrator.schemas.work import StageResult, WorkItem

from .transport import RawResult, _git, _tag_head, subprocess_env, to_stage_result

_MAX_TAIL = 4000  # bounded output tail kept in validation_notes (per command)

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


def _changed_files(cwd: str, base_sha: str) -> list[str] | None:
    """Files changed between ``base_sha`` and the current worktree — committed AND
    uncommitted tracked changes (``git diff --name-only <base>``) plus untracked new files
    (``git ls-files --others``). Returns None on any git error (detection simply doesn't
    fire; the full test run proceeds — a read we can't do is never a false docs-only tag)."""
    diff = _git(cwd, "diff", "--name-only", base_sha)
    if diff.returncode != 0:
        return None
    files = [ln.strip() for ln in diff.stdout.splitlines() if ln.strip()]
    others = _git(cwd, "ls-files", "--others", "--exclude-standard")
    if others.returncode == 0:
        files += [ln.strip() for ln in others.stdout.splitlines() if ln.strip()]
    return files


def classify_change(cwd: str | None, base_sha: str | None) -> str | None:
    """Deterministically classify the task's diff as ``"docs-only"`` or ``"code"`` — or
    None when it can't be determined (missing cwd/base, git error, or an empty diff). Only
    a real git read sets this, so a model can never claim docs-only (the #41 loophole guard
    lives at the fold, but detection itself is git-only by construction). ``"docs-only"``
    requires a NON-EMPTY changeset where every file is documentation; a single non-doc file
    yields ``"code"``."""
    if not cwd or not base_sha:
        return None
    files = _changed_files(cwd, base_sha)
    if not files:  # None (git error) or [] (nothing changed) → undetermined
        return None
    return "docs-only" if all(_is_doc_file(f) for f in files) else "code"


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
        # #5: export the task's per-task port block into the test subprocess so a suite that
        # boots a dev/test server binds THIS task's ports, never a sibling worktree's.
        proc_env = subprocess_env(work)

        # #41: classify the change deterministically (git diff of base_sha..worktree). A
        # docs-only change has no behavioral surface, so a full test run is wasted effort —
        # short-circuit to an HONEST skip (marked skipped: docs-only, never a faked green).
        # The tag also flows into the context plane (ENGINE-lane-only fold) so REVIEW skips
        # test-coverage criteria and the engine won't reject it for lacking new tests.
        change_class = classify_change(work.cwd, (work.context or {}).get("base_sha"))
        if change_class == "docs-only":
            out = {
                "passed": True,
                "failures": [],
                "tests_meaningful": True,  # not judged here — see module docstring / REVIEW
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

        if not commands:
            out = {
                "passed": True,
                "failures": [],
                "tests_meaningful": True,  # not judged here — see module docstring / REVIEW
                "validation_notes": "deterministic test run: no test commands defined "
                                    "(nothing to run); meaningfulness judged by REVIEW.",
                **extra,
            }
            return out, None, True

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
            **extra,
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
