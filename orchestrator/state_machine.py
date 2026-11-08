"""Per-task state machine over the task's own pipeline (target.md §3 / §6.1).

Pure logic over a ``Task``: which stage runs next (walking ``task.pipeline`` — the
stage enum is a vocabulary, not a sequence; 2026-07-01 design pass §1), how to apply
a ``StageResult``, and where to resume after a crash. The ``started_at``-always-present
schema makes the crash marker unambiguous.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Sequence
from typing import NamedTuple

from pydantic import ValidationError

from .file_contention import normalize_claims, relax_claims
from .schemas.enums import (
    STAGE_ORDER,
    Effort,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
    StageStatus,
)
from .schemas.status import ResumeCursor, StageRecord, Task
from .schemas.work import StageResult


def plan_pipeline(task: Task) -> None:
    """Mark every vocabulary stage not in the task's pipeline as ``skipped`` (idempotent)."""
    active = set(task.pipeline)
    for stage, rec in task.stages.items():
        if stage not in active and rec.status is StageStatus.PENDING:
            rec.status = StageStatus.SKIPPED


def _active_sequence(task: Task) -> tuple[Stage, ...]:
    return tuple(task.pipeline)


def next_stage(task: Task) -> Stage | None:
    """First in-pipeline stage whose status is not completed/skipped, else None."""
    plan_pipeline(task)
    for stage in _active_sequence(task):
        if task.stages[stage].status in (StageStatus.COMPLETED, StageStatus.SKIPPED):
            continue
        return stage
    return None


def is_done(task: Task) -> bool:
    return next_stage(task) is None


def begin_stage(
    task: Task,
    stage: Stage,
    *,
    now: str,
    model: str,
    attempt: int = 0,
    effort: Effort | None = None,
) -> None:
    """Mark a stage running and point the resume cursor at it.

    Clears completed_at/error from any prior attempt so a RUNNING record with a
    null completed_at is an unambiguous crash marker (resume_point relies on this).
    Also clears the task-level ``last_error`` (#213) so a stale prior-attempt error
    can't leak into a later reader across a review-fix cycle.

    Stamps ``effort`` (#96/#138/#139) alongside ``model`` so the dispatched reasoning
    effort is durable on the stage record even before the result returns — a crash
    between dispatch and result, or an abandoned dispatch (which has no runner-echoed
    StageResult), still attributes the effort the stage was running at.
    """
    rec = task.stages[stage]
    rec.status = StageStatus.RUNNING
    rec.started_at = now
    rec.completed_at = None
    rec.error = None
    # #213/#184: clear the TASK-level last_error at attempt start too. apply_result sets
    # task.last_error on a FAILED result and _apply_review_rejection sets it when a review
    # cycle parks the task; nothing cleared it, so across a review-fix cycle a later reader
    # (e.g. the BLOCKED_ON_HUMAN notification reason) could surface a stale prior error.
    # Resetting here — the start of every fresh stage attempt — kills that class at the
    # source; apply_result re-sets it if THIS attempt fails.
    task.last_error = None
    rec.attempt = attempt
    rec.model = model
    # #172 assignment convention: validate_assignment on the status models coerces a
    # bare "high" to Effort.HIGH right here (and rejects an invalid string) — no
    # explicit Effort(...) wrap needed. mypy sees only the declared Effort field type
    # and can't model the runtime coercion, so the str arm needs a targeted ignore.
    rec.effort = effort  # type: ignore[assignment]
    task.current_stage = stage
    task.resume_cursor = ResumeCursor(stage=stage, hint=f"{stage.value} running (attempt {attempt})")
    task.updated_at = now


class FoldNotices(NamedTuple):
    """What a fold DROPPED, returned to the caller instead of vanishing (#201/#289).

    The fold layer is pure — it never emits — so each list here is a to-be-emitted audit
    record the engine call site turns into one warning-grade event:

    * ``pr_fields``   → ``pr_field_dropped``      (a malformed pr_* value, #201)
    * ``truncations`` → ``context_value_truncated`` (a capped context value, #289)
    * ``evictions``   → ``context_key_evicted``     (a ceiling-shed context key, #289)
    * ``file_claims`` → ``scope_file_claim_dropped`` (an unusable declared path, #377)
    * ``relaxed_claims`` → ``file_claim_mode_relaxed`` (a parallel-safe path moved to
      ``append``, #628); a change rather than a drop, but never silent either

    Kept as separate lists rather than one, so the call site needs no per-notice branching
    and each event's payload stays a flat, self-describing shape."""

    pr_fields: list[dict[str, str]]
    truncations: list[dict[str, object]]
    evictions: list[dict[str, object]]
    file_claims: list[dict[str, object]]
    relaxed_claims: list[dict[str, object]]


def _no_notices() -> FoldNotices:
    """An empty FoldNotices (fresh lists — never a shared mutable default)."""
    return FoldNotices(
        pr_fields=[], truncations=[], evictions=[], file_claims=[], relaxed_claims=[],
    )


def apply_result(
    task: Task,
    result: StageResult,
    *,
    now: str,
    cost_usd: float | None,
    metered: bool = True,
    iteration: int | None = None,
    parallel_safe_paths: Sequence[str] = (),
) -> FoldNotices:
    """Fold a StageResult into the task's stage record (status + attribution).

    ``cost_usd`` and ``metered`` travel TOGETHER (#319) — they are the ledger row's own
    two fields and mean nothing apart. ``metered=False`` says the cost is an unknown, not
    a measured zero, so a consumer of the task doc can render it honestly instead of as a
    confident ``$0.0000``. Defaulting to True keeps every existing call site's meaning.

    ``parallel_safe_paths`` is the project adapter's glob list (#628), passed in so the fold
    stays pure; only a SCOPE result reads it.

    Returns the drop notices produced by ``_absorb_outputs`` (all empty on the
    non-SUCCESS branch, which folds nothing) so the engine can emit the warning-grade
    audit events — the fold itself stays a pure, sink-free function and the caller owns
    the I/O (#201/#289, honouring the 'never silent' convention)."""
    rec: StageRecord = task.stages[result.stage]
    rec.completed_at = now
    rec.model = result.model
    # #139: fold the ran-at effort, mirroring model; the #172 assignment convention
    # (validate_assignment) coerces the result's bare string to the Effort enum. mypy
    # can't model that runtime coercion, so the str→Effort write needs a targeted ignore.
    rec.effort = result.effort  # type: ignore[assignment]
    rec.provider = result.lane_used.provider
    rec.lane = result.lane_used.execution_mode
    rec.cost_usd = cost_usd
    rec.metered = metered
    rec.input_tokens = result.token_usage.input
    rec.output_tokens = result.token_usage.output
    rec.output = result.structured_output
    rec.attempt = result.attempt
    if iteration is not None:
        rec.iteration = iteration

    notices = _no_notices()
    if result.status is ResultStatus.SUCCESS:
        rec.status = StageStatus.COMPLETED
        rec.error = None
        notices = _absorb_outputs(task, result, parallel_safe_paths)
    else:
        rec.status = StageStatus.FAILED
        rec.error = result.error or result.status.value
        task.last_error = rec.error

    task.current_stage = result.stage
    task.updated_at = now
    return notices


# #564: the context key naming an OPTIMIZE revert and its cause. A fix cycle re-runs OPTIMIZE, so
# a key that only ever overwrote would keep reporting an old revert after a pass that kept its
# commit; the fold and the non-code skip clear it instead.
OPTIMIZE_REVERT_KEY = "optimize_reverted"

# Engine-owned context-fold whitelist (2026-07-01 context-plane design note). Per stage,
# the generic stage-contract keys folded into task.context for downstream prompts. Only
# these keys (present in the canonical schemas/stages/*.json) are folded — never whole
# blobs — so nothing project-specific leaks into the engine's context. The map is
# INJECTIVE across stages (no two stages write the same context key); enforced by test.
CONTEXT_KEYS: dict[Stage, tuple[str, ...]] = {
    Stage.INTAKE: ("branch", "worktree", "base_sha", "baseline_failures",
                   # #607: failed at base but passed on one re-run — flaky, not broken.
                   "baseline_flaky",
                   "port_base", "port_count", "composed_deps",
                   # #385: uncommitted work a REUSED worktree inherited from a previous,
                   # dead run. Folded for the same reason baseline_failures is — it is
                   # state the stage would otherwise discover by destroying it.
                   "inherited_changes", "inherited_changes_note"),
    # ``performance``: SCOPE's speed-design note, so IMPLEMENT builds with it and OPTIMIZE
    # classifies impact from it. It is a prose STRING, so it is bounded by the whole-value
    # cap (``_MAX_STR``, sized for prose), never by the 500-char incidental-list-item cap;
    # ``_ITEM_CAP_BY_KEY`` still names it so a list-shaped note gets prose-sized items too.
    Stage.SCOPE: ("plan", "blocked_reason", "performance"),
    Stage.IMPLEMENT: ("files_changed", "summary"),
    Stage.SIMPLIFY: (),
    # #519: the suggestions the engine files are read from the stage RECORD at finalize, and
    # the model's measurements decide nothing, so neither is folded. #564: the one fact a later
    # stage must act on is that the engine took the pass's commit back off the branch, so TEST,
    # DELIVER and REVIEW do not describe code that is gone. Engine-authored: the benchmark gate
    # strips any model value, and the fold clears it when a later pass reports no revert.
    Stage.OPTIMIZE: (OPTIMIZE_REVERT_KEY,),
    Stage.TEST: ("failures", "validation_notes", "change_class", "test_scope"),
    Stage.DELIVER: ("pr_number", "pr_url"),
    Stage.REVIEW: ("issues",),
}

# Engine-INJECTED context keys (#72): folded into task.context by the engine directly
# (not from a stage's structured output), so they are EXEMPT from the injective stage-write
# map above and rendered/framed separately. ``prior_learnings`` is the cross-run KB recall
# the engine folds at intake. Advisory ("may or may not apply"), so it is shed FIRST when
# the context ceiling is exceeded — durable stage-derived context always outranks it.
ENGINE_INJECTED_KEYS: tuple[str, ...] = ("prior_learnings",)

# Context keys only a DETERMINISTIC ENGINE-lane runner is trusted to fold (#41): the
# docs-only change tag gates downstream effort (lighter TEST, relaxed REVIEW criteria, no
# missing-tests rejection), so a MODEL claiming ``change_class: docs-only`` must be ignored —
# only a git-diff by the engine lane may set it. A non-ENGINE result carrying such a key has
# it dropped at the fold, closing the loophole at the single choke point.
# #558: ``test_scope`` likewise — an "impacted" scope tells REVIEW the green was on a
# subset, so only the engine-lane runner that actually chose it may record it.
DETERMINISTIC_ONLY_KEYS: frozenset[str] = frozenset({"change_class", "test_scope"})

# Bounds so the context (fed into every later prompt) stays bounded regardless of what a
# model returns. Deterministic (no wall-clock/random) → replay reproduces the same fold.
_MAX_STR = 2000  # a single string value
_MAX_ITEM_STR = 500  # a string element inside a list value (the default per-item cap)
_MAX_LIST = 40  # elements kept from a list value
_MAX_CONTEXT_BYTES = 16_384  # whole-context ceiling
_MAX_DROPPED_VALUE = 200  # a dropped pr_* value / its reason, in a drop notice (#201)
# #591: Task.baseline_lint entries kept. Generous because a repo can carry hundreds of old lint
# errors, and bounded because the task doc is rewritten on every update. An entry past the cap
# is reported, and its finding reads as new at REVIEW, which blocks as it did before #591.
_MAX_BASELINE_LINT = 2000

# #289: the per-item cap is chosen by what a FIELD MEANS, not by the value's python type.
# A SCOPE ``plan`` is an ordered list of prose subtask INSTRUCTIONS — routinely well over
# 500 chars each, with the caveats in the tail — so the incidental-list-item cap (meant for
# file paths / short findings) was cutting the highest-value sentence out of every dispatched
# implement prompt, with no space pressure at all (a 9-subtask plan is ~3.6KB against a 16KB
# ceiling). Plan items get a cap sized to the ceiling instead; the whole-context ceiling
# below still binds, so this is not unbounded growth.
_MAX_PLAN_ITEM_STR = 4000
_ITEM_CAP_BY_KEY: dict[str, int] = {
    "plan": _MAX_PLAN_ITEM_STR,
    # SCOPE's speed-design note is prose too. The schema says string (bounded by _MAX_STR);
    # this keeps a model that returns it as a list of sentences from losing their tails.
    "performance": _MAX_STR,
}


def _item_cap(key: str) -> int:
    """The per-item cap for a list value's string elements, by FIELD MEANING (#289)."""
    return _ITEM_CAP_BY_KEY.get(key, _MAX_ITEM_STR)


def _cap_item(x: object, limit: int) -> tuple[object, int]:
    """Bound one string element of a list value.

    Returns ``(value, dropped_chars)`` — ``dropped_chars`` is 0 when nothing was cut, so
    the caller can surface the truncation instead of losing it silently (#289)."""
    if isinstance(x, str) and len(x) > limit:
        return x[:limit] + " … [truncated]", len(x) - limit
    return x, 0


def _cap_value(key: str, v: object) -> tuple[object, list[dict[str, object]]]:
    """Bound one folded value (string/list); scalars pass through unchanged.

    Returns ``(value, notices)``: one bounded notice per thing this cap actually dropped
    (a whole-string cut, a per-item cut naming the element index, or the list tail), each
    naming the ``field`` and how much went. The function stays PURE — it emits nothing and
    takes no sink; only the engine call site turns a notice into an audit event (#201/#289
    'never silent' convention). Deterministic: no wall-clock/random, so replay reproduces
    the same values AND the same notices."""
    if isinstance(v, str):
        if len(v) > _MAX_STR:
            return v[:_MAX_STR] + " … [truncated]", [{
                "field": key,
                "part": "value",
                "kept_chars": _MAX_STR,
                "dropped_chars": len(v) - _MAX_STR,
            }]
        return v, []
    if isinstance(v, list):
        limit = _item_cap(key)
        notices: list[dict[str, object]] = []
        capped: list[object] = []
        for index, item in enumerate(v[:_MAX_LIST]):
            bounded, dropped_chars = _cap_item(item, limit)
            capped.append(bounded)
            if dropped_chars:
                notices.append({
                    "field": key,
                    "part": f"item[{index}]",
                    "kept_chars": limit,
                    "dropped_chars": dropped_chars,
                })
        if len(v) > _MAX_LIST:
            dropped_items = len(v) - _MAX_LIST
            capped.append(f"… ({dropped_items} more)")
            notices.append({
                "field": key,
                "part": "list_tail",
                "kept_items": _MAX_LIST,
                "dropped_items": dropped_items,
            })
        return capped, notices
    return v, []  # bool / int / float / None


def _context_bytes(context: dict) -> int:
    return len(json.dumps(context, default=str, ensure_ascii=False).encode("utf-8"))


def _enforce_context_ceiling(task: Task) -> list[dict[str, object]]:
    """Keep task.context under the whole-context ceiling by a per-KEY size-weighted
    sweep, heaviest-first: each pass evicts the single folded key that weighs the most
    bytes, so a fat key is shed while its small stage-siblings survive (dropping a
    near-ceiling ``test.failures`` no longer takes ``change_class`` /
    ``validation_notes`` down with it — whole-stage eviction was needlessly coarse).
    Ties break reverse-pipeline (review's keys first, intake's last) then the fixed key
    order within that stage's ``CONTEXT_KEYS`` tuple — downstream stages need the earliest
    stages' context most. Deterministic: only json byte-lengths and the fixed enum/tuple
    order decide, never context insertion order.

    Returns one bounded notice per EVICTED key (``field`` + the ``bytes`` it weighed),
    in eviction order, so the caller can surface the loss as an audit event (#289). The
    eviction ORDER and the resulting context are unchanged — this only stops the drop
    from being invisible. Pure: no sink, no wall-clock."""
    evicted: list[dict[str, object]] = []
    if _context_bytes(task.context) <= _MAX_CONTEXT_BYTES:
        return evicted

    # Advisory engine-injected context (prior_learnings, #72) is the first to shed under
    # pressure: it "may or may not apply", so durable stage-derived context outranks it.
    for key in ENGINE_INJECTED_KEYS:
        if _context_bytes(task.context) <= _MAX_CONTEXT_BYTES:
            return evicted
        if key in task.context:
            evicted.append({"field": key, "bytes": _context_bytes({key: task.context[key]})})
        task.context.pop(key, None)

    # A key's weight (json bytes of ``{key: value}``) depends only on that key's own value,
    # never on what else is in the context, so it is STABLE across evictions. Compute each
    # weight once and settle the eviction order up front, instead of re-serializing every
    # candidate on every pass — that was ~O(stages^2) serializations (#26). The candidate
    # list is built reverse-pipeline then intra-tuple, and ``list.sort`` is stable and keeps
    # that order for equal weights, so heaviest-first eviction breaks weight-ties by the
    # latest-pipeline stage's key first, then the first key in that stage's CONTEXT_KEYS
    # tuple — byte-for-byte the same order the old max()-per-pass produced.
    ordered = [
        key
        for stage in reversed(STAGE_ORDER)
        for key in CONTEXT_KEYS[stage]
        if key in task.context
    ]
    ordered.sort(key=lambda key: _context_bytes({key: task.context[key]}), reverse=True)

    for key in ordered:
        if _context_bytes(task.context) <= _MAX_CONTEXT_BYTES:
            return evicted
        if key in task.context:
            evicted.append({"field": key, "bytes": _context_bytes({key: task.context[key]})})
        task.context.pop(key, None)
    return evicted


def _bound_dropped_value(value: object) -> str:
    """Bound a dropped pr_* value to a fixed-length repr for the drop notice, so an
    oversized/adversarial model value can't bloat the emitted audit event. ``repr`` keeps
    the type visible (``''`` vs ``0`` vs ``None``) and is deterministic for the scalars a
    pr_* field carries."""
    text = repr(value)
    if len(text) > _MAX_DROPPED_VALUE:
        return text[:_MAX_DROPPED_VALUE] + " … [truncated]"
    return text


def _short_validation_reason(exc: ValidationError) -> str:
    """A short, deterministic reason string from a field-validation failure (first error's
    message, bounded). pydantic error messages are stable, so this replays identically."""
    errors = exc.errors()
    reason = str(errors[0]["msg"]) if errors else "validation failed"
    if len(reason) > _MAX_DROPPED_VALUE:
        return reason[:_MAX_DROPPED_VALUE] + " … [truncated]"
    return reason


def _absorb_outputs(
    task: Task, result: StageResult, parallel_safe_paths: Sequence[str] = ()
) -> FoldNotices:
    """Fold a stage's well-known structured-output fields into task.pr_* and the
    engine-owned task.context plane (2026-07-01 design note). Fold is tolerant (a
    missing whitelisted key is skipped; a pr_* value that fails field validation is
    dropped rather than raised or stored, #172) and idempotent (a stage succeeds once;
    re-folding the same result yields the same values).

    Returns a ``FoldNotices`` of everything this fold dropped — malformed pr_* values
    (#201), values the per-field caps truncated and keys the whole-context ceiling
    evicted (#289) — so the caller can surface each as an audit event instead of losing
    it silently. The fold stays pure — it emits nothing itself."""
    out = result.structured_output or {}
    dropped: list[dict[str, str]] = []
    truncations: list[dict[str, object]] = []
    # Dedicated pr_* fields stay: other consumers read them (_on_task_completed, status()).
    # Tolerant here too, per the #172 assignment convention: validate_assignment rejects a
    # malformed model-produced value (e.g. pr_number="") AT this write — skip it rather
    # than crash record(). Before #172 the garbage landed silently and made the stored doc
    # unloadable on its next read; dropping the bad value is strictly safer. #201: record
    # each drop so the engine can emit a warning-grade event (drop is no longer invisible).
    if result.stage is Stage.DELIVER:
        for field in ("pr_number", "pr_url"):
            if field in out:
                try:
                    setattr(task, field, out[field])
                except ValidationError as exc:
                    dropped.append({
                        "field": field,
                        "value": _bound_dropped_value(out[field]),
                        "reason": _short_validation_reason(exc),
                    })
    # Generalized fold: every whitelisted key present in the result, bounded.
    engine_lane = result.lane_used.execution_mode is ExecutionMode.ENGINE
    for key in CONTEXT_KEYS.get(result.stage, ()):
        if key not in out:
            continue
        # #41 loophole guard: a deterministic-only key (change_class) folds ONLY from the
        # ENGINE lane — a model result claiming it is ignored (dropped here).
        if key in DETERMINISTIC_ONLY_KEYS and not engine_lane:
            continue
        bounded, cap_notices = _cap_value(key, out[key])
        task.context[key] = bounded
        truncations.extend(cap_notices)
    # #564: a pass that reports no revert ends the previous pass's note, or a fix cycle would
    # keep telling later stages about a commit that this pass has moved past.
    if result.stage is Stage.OPTIMIZE and OPTIMIZE_REVERT_KEY not in out:
        task.context.pop(OPTIMIZE_REVERT_KEY, None)
    # #377: the SCOPE-declared edit surface is a SCHEDULING input, so it folds onto its own
    # durable task field rather than into the context plane — a context key can be evicted
    # by the whole-context ceiling, and an evicted claim would silently un-serialize the
    # run. Normalization is total (a malformed declaration yields no claims, never an
    # exception into record()) and every rejected path comes back as a notice.
    file_claims: list[dict[str, object]] = []
    relaxed_claims: list[dict[str, object]] = []
    if result.stage is Stage.SCOPE and "files" in out:
        declaration = normalize_claims(out["files"])
        file_claims = declaration.notices
        task.scope_files = list(declaration.claims)
        # #426: only the non-default (append) paths are persisted, so an absent entry —
        # including every task doc written before #426 — reads as ``rewrite`` and gates
        # exactly as it did before. #628: a path the adapter marked parallel-safe is stored
        # as append whatever the model wrote, and the move comes back as a notice.
        relaxation = relax_claims(declaration.claims, declaration.modes, parallel_safe_paths)
        relaxed_claims = relaxation.relaxed
        task.scope_file_modes = relaxation.modes
    # #591: what the lint/type tools already report at base. A durable task field, not a context
    # key: the context plane caps a list at 40 items (the rest would read as new errors) and
    # feeds every later prompt. ENGINE lane only, so a model cannot excuse its own errors.
    if result.stage is Stage.INTAKE and engine_lane and "baseline_lint" in out:
        raw_lint = out["baseline_lint"]
        entries = (
            [e for e in raw_lint if isinstance(e, str)] if isinstance(raw_lint, list) else []
        )
        task.baseline_lint = entries[:_MAX_BASELINE_LINT]
        if len(entries) > _MAX_BASELINE_LINT:
            truncations.append({
                "field": "baseline_lint",
                "part": "list_tail",
                "kept_items": _MAX_BASELINE_LINT,
                "dropped_items": len(entries) - _MAX_BASELINE_LINT,
            })
    evictions = _enforce_context_ceiling(task)
    return FoldNotices(
        pr_fields=dropped, truncations=truncations, evictions=evictions,
        file_claims=file_claims, relaxed_claims=relaxed_claims,
    )


_PR_URL_RE = re.compile(r"^https?://\S+/pull/(\d+)/?$")


def pr_not_opened(structured_output: dict | None) -> str | None:
    """Veto reason when a SUCCESS DELIVER did not actually open a PR — else ``None`` (#351).

    ``pr_url`` is free text, so before this check a *compare* link
    (``…/pull/new/task/259``) and a prose sentence beginning ``"blocked: GitHub API
    unreachable…"`` both passed as a delivered PR, with ``pr_number: 0`` sitting right
    beside them. Every DELIVER stage of ``batch-codex-3`` reported success that way and the
    run finished green having opened nothing; the only trace was the string itself, in a
    per-stage JSON no one reads on a passing run.

    A real delivery is a positive ``pr_number`` AND a ``pr_url`` ending in ``/pull/<n>``
    that AGREES with it — the pair has to be coherent, since either alone can be stale from
    a prior fix cycle. Pure: the engine call site turns the reason into the veto and the
    event.
    """
    out = structured_output or {}
    number = out.get("pr_number")
    url = out.get("pr_url")
    detail = f"pr_number={number!r} pr_url={_bound_dropped_value(url)}"
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return (
            "deliver gate: no pull request was opened — a delivered stage must report a "
            f"positive pr_number ({detail}). The branch may already be pushed; open the PR "
            "for it (or re-run once the GitHub API is reachable) rather than reporting a "
            "compare link or an error message as the PR."
        )
    if not isinstance(url, str) or (m := _PR_URL_RE.match(url.strip())) is None:
        return (
            "deliver gate: pr_url is not a pull-request URL — it must end in /pull/<number>, "
            f"not a compare link or a message ({detail})."
        )
    if int(m.group(1)) != number:
        return f"deliver gate: pr_url and pr_number disagree ({detail})."
    return None


def remote_branch_sha(ls_remote_stdout: str, branch: str) -> str | None:
    """The SHA ``git ls-remote origin refs/heads/<branch>`` printed for ``branch`` — else
    ``None`` (branch absent on the remote, or output that names no such ref). Pure (#500)."""
    want = f"refs/heads/{branch}"
    for line in (ls_remote_stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == want and re.fullmatch(r"[0-9a-f]{7,64}", parts[0]):
            return parts[0]
    return None


def keep_open_violation(issue: int, closing_issues: Collection[int]) -> str | None:
    """Reason a keep-open task's PR is wrong when GitHub would close its issue — else
    ``None`` (#613).

    ``closing_issues`` is GitHub's own answer (the PR's closing references), read by the
    engine, so this post-condition is one the model does not author: on ``pred-1141-1291``
    PRs said ``Closes`` with "do not close it on merge" in their own context. Pure: the
    engine call sites turn it into the veto or the completion finding, and the event.
    """
    if issue not in closing_issues:
        return None
    return (
        f"keep-open check: issue #{issue} stays open after this PR, but GitHub lists it as "
        "an issue the PR closes on merge. Edit the PR description (`gh pr edit`) to say "
        f"'Part of #{issue}' with no closing keyword (a form of close, fix or resolve) in "
        f"front of #{issue}, and remove any manual link to it from the PR's Development "
        "section."
    )


def deliver_push_mismatch(checkpoint_sha: str, remote_sha: str | None, branch: str) -> str | None:
    """Veto reason when DELIVER's checkpoint is not what the remote branch holds — else
    ``None`` (#500).

    On ``ff-v1-b32`` task 254 a DELIVER agent probed with ``git fetch origin task/254``,
    which updates ``FETCH_HEAD`` but NOT the stale ``origin/task/254`` tracking ref, read two
    "unpushed commit" reports as "already up to date", and recorded success without
    pushing; the PR sat one commit behind the delivered work. ``remote_sha`` comes from an
    engine-run ``git ls-remote`` (no local tracking state to go stale), so this post-condition
    is one the model does not author. Pure: the engine call site turns it into the veto and
    the event.
    """
    if remote_sha and remote_sha == checkpoint_sha:
        return None
    seen = (
        f"origin/{branch} is at {remote_sha}" if remote_sha
        else f"origin has no branch {branch}"
    )
    return (
        f"deliver gate: the delivered commit {checkpoint_sha} is not on the remote — {seen}, "
        "so the PR does not show the delivered work. Run `git push origin "
        f"{branch}` and confirm with `git ls-remote origin refs/heads/{branch}`; a "
        "remote-tracking ref (`origin/<branch>`, `git log origin/...`) can be stale after a "
        "bare-refspec `git fetch` and is not proof of a push."
    )


def no_test_surface(task: Task) -> bool:
    """#41/#168: does this task have NO test surface for REVIEW's tests-meaningful judgment
    (#13/#541) to bite on?

    True on either of two signals, both fixed at ``add_task`` time or set only by the
    deterministic ENGINE lane — so a model can never self-exempt:

    (a) #41 the change was deterministically classified ``docs-only`` (the git-diff tag; a
        model claim is dropped at the fold, see ``DETERMINISTIC_ONLY_KEYS``);
    (b) #168 the pipeline has no TEST stage at all (micro).

    TEST running on the ENGINE lane is deliberately NOT a signal (#541): that is now every
    lane's default, and a model (IMPLEMENT) still wrote the tests REVIEW judges.

    Pure (a function of the task doc only). Lives here, next to the fold, because the
    engine's ``_review_verdict`` exemption, ``review_proof_gate`` and
    ``unjudged_tests_notice`` below must read the SAME predicate — spellings would drift.
    """
    return (
        task.context.get("change_class") == "docs-only"
        or Stage.TEST not in task.pipeline
    )


def proving_mutation_count(output: dict) -> int:
    """How many of a REVIEW output's ``mutation_checks`` actually failed a test: a named
    mutation with at least one failing test id. A survived mutation is a real record, but it
    proves nothing in favor of the tests. Tolerant of any shape, since a model wrote it."""
    checks = output.get("mutation_checks")
    if not isinstance(checks, list):
        return 0
    return sum(
        1 for check in checks
        if isinstance(check, dict)
        and str(check.get("mutation") or "").strip()
        and isinstance(check.get("failed_tests"), list)
        and any(str(test).strip() for test in check["failed_tests"])
    )


class SurvivorDisposition(NamedTuple):
    """What ``surviving_mutant_disposition`` made of a REVIEW's survived mutations (#614).

    ``blocking``: structured review issues (``review.json`` issue-object shape, tagged
    ``source="mutation_check"`` and carrying the verbatim ``mutation``) for every survivor the
    reviewer flagged ``blocking: true`` WITH a reason. ``noted``: ``non_blocking`` entries
    (``disposition="drop"``) for every other survivor. ``demoted``: survivors flagged blocking
    without a reason, which the engine refuses to cycle on and events instead.
    """

    blocking: list[dict]
    noted: list[dict]
    demoted: list[dict]


_MUTATION_LOCATION = re.compile(r"^\s*(?P<file>[^\s:]+?)(?::(?P<line>\d+))?(?=[\s:]|$)")
# The verbatim mutation string is the re-review's lookup key, so it is kept whole up to a
# bound that still fits the convergence fingerprint's 160-code-point window with the prefix.
_MAX_MUTATION_TEXT = 120
# A ``non_blocking`` title (the fingerprint key at finalize) is cut where the filing code cuts.
_MAX_NOTED_TITLE = 80


def _is_blocking_flag(value: object) -> bool:
    """``blocking: true`` as a model may actually spell it (a bool, or the string)."""
    return value is True or (isinstance(value, str) and value.strip().casefold() == "true")


def _mutation_text(check: dict) -> str:
    """One check's ``mutation`` string, whitespace-collapsed and bounded; ``""`` when
    absent or blank (a model may omit it), so callers can skip the check in one test."""
    return re.sub(r"\s+", " ", str(check.get("mutation") or "")).strip()[:_MAX_MUTATION_TEXT]


def surviving_mutant_disposition(output: dict) -> SurvivorDisposition:
    """The stopping rule for surviving mutants (#614), as a pure walk over a REVIEW output's
    ``mutation_checks``.

    A reviewer proves ``tests_meaningful`` by mutating the change and watching tests fail.
    Mutations the tests MISS are real records, but each review samples different ones, so
    treating any survivor as a blocking finding opened a full implement-through-review cycle
    per round on code that was correct every time (pred-1141-1291 #1254/#1310, pred-1396-1382
    #1378). The rule, decided here rather than by the prompt: a survivor opens a cycle only
    when the reviewer flags it ``blocking: true`` AND names a ``blocking_reason`` (the spec
    requirement or money-path/safety guard it falls on). A flag without a reason is DEMOTED,
    never promoted — an unexplained block is the hydra this exists to stop. Everything else is
    a noted gap, reported once in ``non_blocking`` and never filed.

    Tolerant of any shape, since a model wrote it: a survivor is a named mutation with no
    recorded failing test (the exact complement of ``proving_mutation_count``), so a check
    whose ``failed_tests`` is missing or malformed proves nothing and is handled as one.
    """
    checks = output.get("mutation_checks")
    if not isinstance(checks, list):
        return SurvivorDisposition([], [], [])
    blocking: list[dict] = []
    noted: list[dict] = []
    demoted: list[dict] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        mutation = _mutation_text(check)
        if not mutation:
            continue
        failed = check.get("failed_tests")
        if isinstance(failed, list) and any(str(t).strip() for t in failed):
            continue  # a proving mutation; not this rule's business
        reason = check.get("blocking_reason")
        reason = re.sub(r"\s+", " ", reason).strip()[:300] if isinstance(reason, str) else ""
        if _is_blocking_flag(check.get("blocking")):
            if not reason:
                demoted.append({
                    "mutation": mutation,
                    "reason": "flagged blocking with no blocking_reason — noted, not cycled",
                })
                noted.append(_noted_gap(mutation, ""))
                continue
            issue: dict = {
                "severity": "important",
                "description": f"surviving mutant: {mutation} — {reason}",
                "suggested_fix": (
                    "add or sharpen a test that fails on exactly this mutation, then re-apply "
                    "it to confirm"
                ),
                "source": "mutation_check",
                "mutation": mutation,
            }
            located = _MUTATION_LOCATION.match(mutation)
            if located and ("/" in located["file"] or "." in located["file"]):
                issue["file"] = located["file"]
                if located["line"] is not None:
                    issue["line"] = int(located["line"])
            blocking.append(issue)
        else:
            noted.append(_noted_gap(mutation, reason))
    return SurvivorDisposition(blocking, noted, demoted)


def _noted_gap(mutation: str, reason: str) -> dict:
    """The ``non_blocking`` entry for a survivor that opens no cycle: a ``drop``
    disposition (noted in the completion note, never filed), titled by the mutation so the
    engine can dedupe it against an entry the reviewer already wrote, with the reviewer's
    own note appended when one was given."""
    detail = (
        f"The mutation `{mutation}` survived the covering tests (no test failed on it). "
        "The reviewer did not place it on spec-required behaviour or a money-path/safety "
        "guard, so under the #614 stopping rule it is noted once and opens no fix cycle."
    )
    if reason:
        detail += f" Reviewer note: {reason}"
    return {
        "title": f"surviving mutant: {mutation}"[:_MAX_NOTED_TITLE],
        "detail": detail,
        "disposition": "drop",
    }


def unchecked_named_survivors(open_mutants: Collection[str], output: dict) -> list[str]:
    """Which of a fix cycle's named survivors this re-review did NOT re-apply (#614).

    The re-review is told to copy each named ``mutation`` string verbatim into its own
    ``mutation_checks``; the match is on the same normalization the convergence fingerprint
    uses (whitespace-collapsed, case-folded), so cosmetic rewording still counts. Pure; the
    engine events the misses (``named_survivor_unchecked``) and never vetoes on them — a
    reviewer that reads the fix instead of re-running it has still reviewed.
    """
    if not open_mutants:
        return []
    checks = output.get("mutation_checks")
    seen: set[str] = set()
    if isinstance(checks, list):
        for check in checks:
            if isinstance(check, dict):
                text = _mutation_text(check)
                if text:
                    seen.add(_normalize_mutation(text))
    return [m for m in open_mutants if _normalize_mutation(m) not in seen]


def _normalize_mutation(text: str) -> str:
    """The match key for a named survivor: the same whitespace/case fold the convergence
    fingerprint applies, so a re-review's cosmetic rewording of the mutation still counts."""
    return re.sub(r"\s+", " ", str(text)).strip().casefold()[:_MAX_MUTATION_TEXT]


def _not_checked_reason(output: dict) -> str | None:
    """The reviewer's stated reason for not proving the verdict, or None when blank."""
    reason = output.get("tests_not_checked_reason")
    return reason.strip() if isinstance(reason, str) and reason.strip() else None


def review_proof_gate(task: Task, result: StageResult) -> str | None:
    """Why an APPROVING REVIEW must re-run because it did not prove the tests mean anything
    (#541), or None. Pure; the engine downgrades the result to a failure with this as its
    error, so the retry reads it as a learning and the breaker bounds the loop.

    Applies to a successful approval on a change with a test surface. It requires either a
    boolean verdict backed by a mutation that failed a test, or a stated
    ``tests_not_checked_reason``, which is accepted and evented by ``unjudged_tests_notice``
    so an unproven pass stays visible. An explicit ``false`` needs no proof (the verdict
    rejects it anyway), and a rejecting review is not vetoed because it re-runs after the fix.
    """
    if result.stage is not Stage.REVIEW or result.status is not ResultStatus.SUCCESS:
        return None
    out = result.structured_output or {}
    if out.get("approved") is not True or out.get("tests_meaningful") is False:
        return None
    if no_test_surface(task) or _not_checked_reason(out) is not None:
        return None
    verdict = out.get("tests_meaningful")
    if isinstance(verdict, bool) and proving_mutation_count(out):
        return None
    return (
        "review proof gate: the review approved a change with tests but recorded no proof "
        "they mean anything. Apply a mutation to the change's source, run the covering tests, "
        "and report tests_meaningful plus mutation_checks naming the mutation and the tests "
        "that failed on it; if no check can settle it, say why in tests_not_checked_reason."
    )


def unjudged_tests_notice(task: Task, result: StageResult) -> dict[str, object] | None:
    """Did REVIEW actually judge whether this change's tests are meaningful (#261/#541)?

    Returns a notice dict describing what went unjudged, unproven, or judged and then
    discarded, else None. PURE — no wall-clock/random/I/O, per the project's "pure functions
    return what they dropped; the engine emits the event" convention: ``Engine.record`` turns
    a notice into a warning-grade ``test_validation_skipped`` event. It never blocks;
    ``review_proof_gate`` is what blocks, and an approval that reaches here without proof got
    past it by stating a reason, which this event carries as ``stated_reason``.

    ``kind="not_judged"`` — REVIEW returned no boolean. Scoped to a plausible test surface: a
    pipeline that HAS a TEST stage and a change not deterministically classified
    ``docs-only``. A pipeline with no TEST stage (micro) is out of scope — that lane declares
    no test verification at all at ``add_task`` time, which is a recorded lane choice rather
    than a silent skip.

    ``kind="unproven"`` — REVIEW answered ``true`` without a mutation that failed a test.

    ``kind="verdict_suppressed"`` — the reviewer DID judge (an explicit ``false``) and the
    #41/#168 no-test-surface exemption discarded it. Behavior is unchanged (the exemption
    still suppresses); it is no longer silent.
    """
    if result.stage is not Stage.REVIEW or result.status is not ResultStatus.SUCCESS:
        return None
    out = result.structured_output or {}
    reviewed = out.get("tests_meaningful")
    exempt = no_test_surface(task)
    if reviewed is False and exempt:
        return {
            "kind": "verdict_suppressed",
            "test_stage": _test_stage_fact(task),
            "review_reported": False,
            "reason": (
                "the reviewer judged the tests NOT meaningful, but the no-test-surface "
                "exemption (#41/#168) discarded that verdict, so it did not reject the task"
            ),
        }
    if exempt or reviewed is False:
        return None
    stated = _not_checked_reason(out)
    if not isinstance(reviewed, bool):
        return {
            "kind": "not_judged",
            "test_stage": _test_stage_fact(task),
            # Null/absent in the normal case; a non-boolean (a model can return anything
            # there) is bounded, never carried into the event verbatim.
            "review_reported": _bound_report(reviewed),
            "stated_reason": stated[:_MAX_DROPPED_VALUE] if stated else None,
            "reason": (
                "nothing judged whether the tests meaningfully exercise this change: REVIEW "
                "returned no tests_meaningful verdict (fail-OPEN, not blocking)"
            ),
        }
    if proving_mutation_count(out):
        return None
    return {
        "kind": "unproven",
        "test_stage": _test_stage_fact(task),
        "review_reported": True,
        "stated_reason": stated[:_MAX_DROPPED_VALUE] if stated else None,
        "reason": (
            "REVIEW answered tests_meaningful=true without recording a mutation that failed "
            "a test, so the verdict is the reviewer's reading, not a measurement"
        ),
    }


def _bound_report(value: object) -> object:
    """One side's ``tests_meaningful`` as it goes into the audit event: ``None`` stays null
    (the honest abstention this event is about), and anything else — a model may return an
    arbitrary string there, which every gate ignores as non-boolean — becomes a BOUNDED repr,
    reusing #201's drop-notice bound so an oversized value can't bloat the event."""
    return None if value is None else _bound_dropped_value(value)


def _test_stage_fact(task: Task) -> str:
    """How the task's TEST stage ran, for a ``test_validation_skipped`` notice: the
    deterministic ENGINE runner (which cannot judge meaningfulness), a model lane, or
    absent from the pipeline entirely."""
    if Stage.TEST not in task.pipeline:
        return "absent"
    return "engine" if Stage.TEST in task.deterministic_stages else "model"


def reset_for_fix_cycle(task: Task, from_stage: Stage, *, cause: str = "review") -> list[Stage]:
    """Re-open the tail of the pipeline for a fix cycle: every pipeline stage at/after
    ``from_stage`` gets a fresh PENDING record, so ``next_stage`` returns ``from_stage`` and
    the fix re-runs implement→…→review. ``cause`` (what sent it back: ``review`` or ``test``)
    names the cycle in the resume hint; a ``review`` cycle also records the commit that
    review saw (``last_checkpoint.sha``) as ``Task.review_fix_base_sha`` (#564), so the
    cycle's OPTIMIZE can classify the cycle's own diff rather than the whole task's.

    History is not lost — every prior execution is already durable in the per-stage
    logs (``write_stage_log``); the stage RECORD is working state, not the audit trail.
    Returns the stages that were reset (empty when ``from_stage`` is not in the
    pipeline — the caller must handle that as "no fix cycle possible")."""
    if from_stage not in task.pipeline:
        return []
    idx = task.pipeline.index(from_stage)
    reset = list(task.pipeline[idx:])
    for stage in reset:
        task.stages[stage] = StageRecord()
    if cause == "review":
        reviewed = (task.last_checkpoint or {}).get("sha")
        task.review_fix_base_sha = str(reviewed) if reviewed else None
    task.resume_cursor = ResumeCursor(
        stage=from_stage, hint=f"{cause} fix cycle: re-running from {from_stage.value}"
    )
    return reset


def resume_point(task: Task) -> Stage | None:
    """Where to re-enter after a crash.

    A stage left ``running`` (started_at set, completed_at null) is a crash marker —
    re-run it. Otherwise resume at the first non-completed/skipped stage.
    """
    plan_pipeline(task)
    for stage in _active_sequence(task):
        rec = task.stages[stage]
        if rec.status is StageStatus.RUNNING and rec.started_at and not rec.completed_at:
            return stage
        if rec.status in (StageStatus.COMPLETED, StageStatus.SKIPPED):
            continue
        return stage
    return None


def stage_lane_used(result: StageResult) -> tuple[ExecutionMode, Provider]:
    return (result.lane_used.execution_mode, result.lane_used.provider)
