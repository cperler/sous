"""Cross-run learnings knowledge base (#72).

Durable, per-project, append-only JSONL KB: harvest a finished task's learnings at
finalize; deterministically recall relevant prior learnings and fold them into a fresh
task's intake context so a later run doesn't re-pay to learn the same lesson.
"""

from __future__ import annotations

from orchestrator import learnings_kb as kb
from orchestrator.cli import main as cli_main
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ResultStatus, TaskState
from orchestrator.schemas.status import Task
from orchestrator.state_machine import (
    _MAX_CONTEXT_BYTES,
    _context_bytes,
    _enforce_context_ceiling,
)
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, FakeTaskSource, make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


# --- pure KB: append / dedupe -------------------------------------------------------


def test_append_returns_written_and_persists(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    written = kb.append_learnings(path, [{"text": "watch the auth cache", "kind": "manual"}])
    assert len(written) == 1
    entry = written[0]
    assert entry["id"].startswith("lk-") and entry["ts"] and entry["kind"] == "manual"
    assert kb.read_entries(path) == written


def test_dedupe_by_normalized_fingerprint(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    kb.append_learnings(path, [{"text": "test (attempt 0): the db pool leaks"}])
    # Same lesson, different attempt counter + whitespace/case -> same fingerprint, skipped.
    again = kb.append_learnings(path, [{"text": "TEST (attempt 3):   the DB pool leaks"}])
    assert again == []
    # A genuinely different lesson is kept.
    more = kb.append_learnings(path, [{"text": "test (attempt 0): the cache is stale"}])
    assert len(more) == 1
    assert len(kb.read_entries(path)) == 2


def test_dedupe_within_a_single_batch(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    written = kb.append_learnings(
        path, [{"text": "same lesson"}, {"text": "same lesson"}, {"text": "other"}]
    )
    assert len(written) == 2


def test_read_entries_tolerates_corrupt_lines(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    kb.append_learnings(path, [{"text": "good one"}])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{ this is not json\n\n")
    kb.append_learnings(path, [{"text": "good two"}])
    texts = [e["text"] for e in kb.read_entries(path)]
    assert texts == ["good one", "good two"]  # the corrupt/blank lines are skipped


# --- pure KB: relevance scoring -----------------------------------------------------


def test_scoring_tier_order_file_kind_stage_token(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    kb.append_learnings(path, [
        {"text": "TOKEN entry shared delta"},
        {"text": "STAGE entry", "stage": "test"},
        {"text": "KIND entry", "failure_kind": "unit"},
        {"text": "FILE entry", "files": ["pkg/mod/a.py"]},
    ])
    query = {
        "files": ["pkg/mod/b.py"],          # same dir as the FILE entry -> file overlap
        "failure_kind": "unit",             # matches the KIND entry
        "stage": "test",                    # matches the STAGE entry
        "title_tokens": ["shared", "delta"],  # matches the TOKEN entry
    }
    hits = kb.relevant_learnings(path, query, limit=10)
    assert hits == ["FILE entry", "KIND entry", "STAGE entry", "TOKEN entry shared delta"]


def test_recency_breaks_ties(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    kb.append_learnings(path, [
        {"text": "older shared note", "ts": "2026-07-01T00:00:00+00:00"},
        {"text": "newer shared note", "ts": "2026-07-04T00:00:00+00:00"},
    ])
    hits = kb.relevant_learnings(path, {"title_tokens": ["shared", "note"]}, limit=10)
    assert hits == ["newer shared note", "older shared note"]  # newer first on a score tie


def test_no_signal_entries_are_not_returned(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    kb.append_learnings(path, [{"text": "utterly unrelated content here"}])
    assert kb.relevant_learnings(path, {"title_tokens": ["something", "else"]}) == []


def test_limit_and_bounded_text(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    long_text = "overflow " + "x" * 2000
    written = kb.append_learnings(path, [{"text": long_text}])
    assert len(written[0]["text"]) <= kb.MAX_TEXT
    for i in range(6):
        kb.append_learnings(path, [{"text": f"shared token entry number {i}"}])
    hits = kb.relevant_learnings(path, {"title_tokens": ["shared", "token"]}, limit=3)
    assert len(hits) == 3


# --- classification of raw learning strings -----------------------------------------


def test_classify_kinds_and_stage_and_failure_kind() -> None:
    review = "review rejected (cycle 1) — blocking issues: fix the thing"
    assert kb.classify_kind(review) == "review"
    assert kb.extract_stage(review) == "review"

    fail = "implement (attempt 0): compile error\n  failing: test_a [unit]; test_b [unit]"
    assert kb.classify_kind(fail) == "failure"
    assert kb.extract_stage(fail) == "implement"
    assert kb.extract_failure_kind(fail) == "unit"

    infra = "test (attempt 1): infrastructure failure — environment reset (ok), re-running"
    assert kb.classify_kind(infra) == "infra"

    salvage = "implement (attempt 1): the previous attempt COMMITTED work before it failed"
    assert kb.classify_kind(salvage) == "salvage"


# --- harvest at task finalize -------------------------------------------------------


def _run_failed_task(eng, run, task, *, error="boom", attempts=3) -> dict:
    eng.create_run(run)
    eng.add_task(run, task)
    eng.record(run, make_result(eng.next_work(run, task)))  # intake
    eng.record(run, make_result(eng.next_work(run, task)))  # scope
    out = {}
    for i in range(attempts):  # implement fails until attempts exhausted -> FAILED
        w = eng.next_work(run, task)
        out = eng.record(run, make_result(w, status=ResultStatus.FAILURE, error=f"{error} {i}"))
    return out


def test_harvest_fires_event_for_a_task_that_learned(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=9)
    out = _run_failed_task(eng, "r1", "t1", error="db pool leak")
    assert out["task_state"] == "failed"
    harvested = [e for e in eng.store.read_events("r1") if e["type"] == "learnings_harvested"]
    assert harvested and any(e.get("count", 0) >= 1 for e in harvested)
    entries = kb.read_entries(eng._learnings_kb_path())
    assert any("db pool leak" in e["text"] for e in entries)


def test_harvest_skips_a_clean_task(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))
    assert eng.store.load_task("r1", "t1").state.value == "completed"
    events = [e["type"] for e in eng.store.read_events("r1")]
    assert "learnings_harvested" not in events  # a clean first-pass task adds no noise
    assert kb.read_entries(eng._learnings_kb_path()) == []


def test_harvest_never_breaks_finalize_on_a_raising_kb(tmp_path, project, monkeypatch) -> None:
    eng = _engine(tmp_path, project, max_attempts=2, breaker_threshold=9)
    # Force the KB path to be a directory so any read/append raises -> harvest must swallow.
    kb_dir = tmp_path / "kb-as-dir"
    kb_dir.mkdir()
    monkeypatch.setattr(eng, "_learnings_kb_path", lambda: kb_dir)
    out = _run_failed_task(eng, "r1", "t1", attempts=2)
    assert out["task_state"] == "failed"  # finalize survived
    assert eng.store.load_run("r1").state.value == "failed"
    failed_ev = [e for e in eng.store.read_events("r1") if e["type"] == "learnings_harvest_failed"]
    assert failed_ev  # the failure was evented, not raised


def test_retrospective_patterns_are_harvested(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=9, breaker_threshold=2)
    # Same error twice -> breaker plateau -> a recurring pattern in the retrospective.
    out = _run_failed_task(eng, "r1", "t1", error="identical boom", attempts=2)
    assert out["task_state"] == "failed"
    entries = kb.read_entries(eng._learnings_kb_path())
    assert any(e["text"].startswith("recurring failure at") for e in entries)


# --- fold at intake: the two-run money test -----------------------------------------


class _TitledSource(FakeTaskSource):
    def __init__(self, titles: dict[str, str]) -> None:
        super().__init__()
        self._titles = titles

    def resolve(self, task_id: str):
        spec = super().resolve(task_id)
        return spec.model_copy(update={"title": self._titles.get(task_id, spec.title)})


def _titled_project(titles: dict[str, str]) -> FakeProject:
    proj = FakeProject()
    proj._task_source = _TitledSource(titles)
    return proj


def test_cross_run_fold_lands_prior_learnings_in_a_second_run(tmp_path) -> None:
    """THE money test: run 1's task fails and learns; run 2's fresh (related) task folds
    that prior learning into its context at intake — proving the durable cross-run flow."""
    project = _titled_project({
        "t1": "authentication login session handling",
        "t2": "authentication login retry backoff",
    })
    eng = _engine(tmp_path, project, max_attempts=2, breaker_threshold=9)

    # --- Run 1: t1 fails at implement, learning a distinctive lesson, harvested at finalize.
    _run_failed_task(eng, "r1", "t1", error="authentication login token intermittently rejected",
                     attempts=2)
    assert kb.read_entries(eng._learnings_kb_path())  # KB now holds run-1 learnings

    # --- Run 2: a FRESH task, different run, same project KB. Its FIRST stage (intake) folds.
    eng.create_run("r2")
    eng.add_task("r2", "t2")
    intake = eng.next_work("r2", "t2")  # first pipeline stage -> recall + fold happens here
    t2 = eng.store.load_task("r2", "t2")
    prior = t2.context.get("prior_learnings")
    assert prior, "run-2's fresh task should inherit run-1's learnings via the KB"
    assert any("authentication" in p for p in prior)
    # The fold is durable AND renders (hedged) into a later model stage's prompt.
    eng.record("r2", make_result(intake))  # intake ok
    scope_prompt = eng.next_work("r2", "t2").prompt
    assert "Prior cross-run learnings (may or may not apply)" in scope_prompt
    assert "authentication" in scope_prompt


def test_env_escape_hatch_disables_harvest_and_fold(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_NO_LEARNINGS_KB", "1")
    project = _titled_project({"t1": "authentication login", "t2": "authentication login retry"})
    eng = _engine(tmp_path, project, max_attempts=2, breaker_threshold=9)
    _run_failed_task(eng, "r1", "t1", error="authentication login broke", attempts=2)
    # Nothing harvested (feature off).
    assert kb.read_entries(eng._learnings_kb_path()) == []
    eng.create_run("r2")
    eng.add_task("r2", "t2")
    eng.next_work("r2", "t2")
    assert "prior_learnings" not in eng.store.load_task("r2", "t2").context


def test_use_learnings_kb_flag_off_disables_feature(tmp_path) -> None:
    project = _titled_project({"t1": "authentication login", "t2": "authentication login retry"})
    eng = _engine(tmp_path, project, max_attempts=2, breaker_threshold=9, use_learnings_kb=False)
    _run_failed_task(eng, "r1", "t1", error="authentication login broke", attempts=2)
    assert kb.read_entries(eng._learnings_kb_path()) == []


# --- ceiling & path resolution ------------------------------------------------------


def test_prior_learnings_is_shed_first_under_ceiling(tmp_path) -> None:
    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    task.context = {
        "branch": "b",
        "files_changed": ["m" * 480 for _ in range(10)],
        "failures": ["z" * 480 for _ in range(20)],
        "prior_learnings": ["p" * 480 for _ in range(10)],
    }
    assert _context_bytes(task.context) > _MAX_CONTEXT_BYTES
    _enforce_context_ceiling(task)
    assert _context_bytes(task.context) <= _MAX_CONTEXT_BYTES
    assert "prior_learnings" not in task.context  # advisory context sheds first
    assert "branch" in task.context  # durable stage context survives


def test_resolve_kb_path_precedence(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ORCHESTRATOR_LEARNINGS_KB_PATH", raising=False)
    runs_root = tmp_path / "runs"
    # default: <runs-root>/learnings-kb.jsonl
    assert kb.resolve_kb_path(runs_root) == runs_root / "learnings-kb.jsonl"
    # env override wins over the default
    monkeypatch.setenv("ORCHESTRATOR_LEARNINGS_KB_PATH", str(tmp_path / "env.jsonl"))
    assert kb.resolve_kb_path(runs_root) == tmp_path / "env.jsonl"

    # a project override wins over everything
    class _P:
        learnings_kb_path = str(tmp_path / "proj.jsonl")

    assert kb.resolve_kb_path(runs_root, _P()) == tmp_path / "proj.jsonl"


# --- CLI smoke ----------------------------------------------------------------------


def test_cli_kb_add_and_show(tmp_path, capsys) -> None:
    import json

    root = str(tmp_path)
    assert cli_main(["--root", root, "kb", "add", "beware the flaky auth cache",
                     "--kind", "manual"]) == 0
    add_out = json.loads(capsys.readouterr().out)
    assert add_out["ok"] and add_out["added"] == 1

    assert cli_main(["--root", root, "kb", "show"]) == 0
    show_out = json.loads(capsys.readouterr().out)
    assert show_out["count"] == 1
    assert "flaky auth cache" in show_out["entries"][0]["text"]

    assert cli_main(["--root", root, "kb", "show", "--query", "flaky auth cache"]) == 0
    q_out = json.loads(capsys.readouterr().out)
    assert q_out["count"] == 1 and "flaky auth cache" in q_out["learnings"][0]


# --- #384: capacity notices are not learnings, and files must be earned ---------------


def test_capacity_notice_channels_are_asymmetric() -> None:
    """The provider's own first-person notice matches anywhere; the broad status words only
    as the prefix of an engine-authored head line — so a task ABOUT rate limiting survives."""
    # (a) a live limit notice quoted in a failed attempt's output tail
    assert kb.is_capacity_notice(
        "test (attempt 0): failed\n"
        "  output tail: You've hit your session limit · resets 3:50pm (America/New_York)"
    )
    # (b) the status word as the head line's error text
    assert kb.is_capacity_notice('deliver (attempt 0): rate-limited (429 too many requests)')
    # (c) the same, distilled by the retrospective
    assert kb.is_capacity_notice("recurring failure at test (2x): overloaded, retrying")

    # NOT notices: a task whose own subject matter is rate limiting.
    assert not kb.is_capacity_notice(
        "review rejected (cycle 1) — blocking issues: important — src/api/limits.py:12 — "
        "the rate limit retry drops the 429 body before logging it"
    )
    assert not kb.is_capacity_notice(
        "test (attempt 0): failed\n  failing: tests/test_ratelimit.py::test_429 [unit]"
    )
    assert not kb.is_capacity_notice("")


def test_mentioned_files_is_the_texts_own_locus() -> None:
    files = ["src/pkg/cli.py", "src/pkg/registry_import.py", "docs/readme.md"]
    # named by full path (with a line number) and by bare basename
    assert kb.mentioned_files("blew up at src/pkg/cli.py:172 while importing", files) == [
        "src/pkg/cli.py"
    ]
    assert kb.mentioned_files("registry_import.py raised on an empty row", files) == [
        "src/pkg/registry_import.py"
    ]
    # a contentless failure names nothing -> inherits nothing
    assert kb.mentioned_files("implement (attempt 0): failed", files) == []
    # a near-miss basename is not a match
    assert kb.mentioned_files("mycli.py is unrelated", files) == []


def test_capacity_learnings_are_dropped_at_harvest(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    task = Task(task_id="t1", run_id="r1", created_at="x", updated_at="x")
    task.context = {"files_changed": ["src/pkg/cli.py"]}
    task.learnings = [
        "test (attempt 0): failed\n  output tail: You've hit your session limit · resets 3:50pm",
        "implement (attempt 1): NameError in src/pkg/cli.py:12 — the import moved",
    ]
    written = kb.harvest_from_task(path, task, "r1")
    assert [e["text"] for e in written] == [task.learnings[1]]  # the notice never lands


def test_harvest_stamps_only_the_files_a_learning_names(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    task = Task(task_id="t1", run_id="r1", created_at="x", updated_at="x")
    task.context = {"files_changed": [f"src/pkg/mod{i}.py" for i in range(10)]}
    task.learnings = [
        "review rejected (cycle 1) — blocking issues: critical — src/pkg/mod3.py:12 — leak",
        "implement (attempt 0): failed",  # no file locus at all
    ]
    written = kb.harvest_from_task(path, task, "r1")
    assert written[0]["files"] == ["src/pkg/mod3.py"]
    assert written[1]["files"] == []  # no longer inherits the task's whole change list


def test_contentless_failure_no_longer_outranks_a_real_lesson(tmp_path) -> None:
    """The issue's repro: a recall for a task inside the same package must return the
    lessons with substance, not capacity noise wearing an inherited path list."""
    path = tmp_path / "kb.jsonl"
    task = Task(task_id="t9", run_id="r1", created_at="x", updated_at="x")
    task.context = {"files_changed": ["src/pkg/cli.py", "src/pkg/registry_import.py"]}
    task.learnings = [
        "deliver (attempt 0): rate-limited — You've hit your session limit · resets 11:20pm",
        "implement (attempt 0): failed",
        "review rejected (cycle 1) — blocking issues: critical — src/pkg/cli.py:172 — "
        "the importer swallows a partial write",
    ]
    kb.harvest_from_task(path, task, "r1")
    hits = kb.relevant_learnings(
        path,
        {"files": ["src/pkg/other.py"], "stage": "implement", "title_tokens": []},
        limit=5,
    )
    assert hits[0].startswith("review rejected")  # file overlap is earned, not inherited
    assert not any("session limit" in h for h in hits)


def test_legacy_capacity_rows_are_excluded_at_recall(tmp_path) -> None:
    """The KB is append-only, so the rows written before the harvest filter existed can only
    be neutralised at read time."""
    path = tmp_path / "kb.jsonl"
    kb.append_learnings(path, [
        {"text": "test (attempt 0): failed\n  output tail: You've hit your session limit "
                 "· resets 3:50pm (America/New_York)",
         "kind": "failure", "stage": "test",
         "files": [f"src/pkg/mod{i}.py" for i in range(10)]},
        {"text": "implement (attempt 2): the fixture factory needs an explicit tz",
         "kind": "failure", "stage": "implement", "files": ["src/pkg/mod1.py"]},
    ])
    hits = kb.relevant_learnings(path, {"files": ["src/pkg/mod5.py"], "stage": "test"}, limit=5)
    assert hits == ["implement (attempt 2): the fixture factory needs an explicit tz"]


def test_engine_filters_capacity_learnings_and_records_the_drop(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=2, breaker_threshold=9)
    out = _run_failed_task(
        eng, "r1", "t1",
        error="rate-limited: You've hit your session limit · resets 3:50pm", attempts=2,
    )
    assert out["task_state"] == "failed"
    assert kb.read_entries(eng._learnings_kb_path()) == []  # neither task nor retrospective
    harvested = [e for e in eng.store.read_events("r1") if e["type"] == "learnings_harvested"]
    assert harvested and harvested[0]["count"] == 0
    assert harvested[0]["skipped_capacity"] >= 1  # the drop is evented, never silent


def test_retrospective_pattern_without_a_sample_error_is_not_harvested(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=9, breaker_threshold=2)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # scope
    for _ in range(2):  # same (empty) signature twice -> a plateau pattern, no substance
        w = eng.next_work("r1", "t1")
        eng.record("r1", make_result(w, status=ResultStatus.FAILURE, error=None))
    # the retrospective DOES distil a qualifying pattern — it just has nothing to say
    patterns = eng.retrospective("r1")["patterns"]
    assert any(
        (p.get("cross_task") or (p.get("occurrences") or 0) >= 2) and not p.get("sample_error")
        for p in patterns
    )
    entries = kb.read_entries(eng._learnings_kb_path())
    assert not any(e["text"].startswith("recurring failure at") for e in entries)


# --- #401: text cap by KIND, and boundary-aware truncation --------------------------


def test_text_cap_is_chosen_by_kind() -> None:
    """Prose kinds get the larger cap; terse rows and unknown kinds get the default."""
    assert kb.text_cap("review") == kb.MAX_TEXT_PROSE
    assert kb.text_cap("process") == kb.MAX_TEXT_PROSE
    assert kb.text_cap("failure") == kb.MAX_TEXT
    assert kb.text_cap("infra") == kb.MAX_TEXT
    assert kb.text_cap(None) == kb.MAX_TEXT
    assert kb.text_cap("not-a-kind") == kb.MAX_TEXT
    assert kb.MAX_TEXT_PROSE > kb.MAX_TEXT


def test_review_prose_survives_whole_where_the_old_global_cap_cut_it(tmp_path) -> None:
    """The motivating regression (#401): a real rejection detail is ~600 chars, so the old
    500-char global cap truncated it mid-sentence and it recalled as a fragment."""
    path = tmp_path / "kb.jsonl"
    review = (
        "review rejected (cycle 1) — blocking issues: important — orchestrator/engine.py:5281 "
        "— Proposal filing is not concurrency-safe. Two runs can both read an empty ledger and "
        "call file_followup before either reaches the locked append_filing; the second append "
        "is suppressed, but two tracker issues already exist. important — "
        "orchestrator/learnings_kb.py:182 — Process deduplication keys only on normalized text "
        "and run_id, omitting the target, so identical observations about different artifacts "
        "collapse into one row."
    )
    assert kb.MAX_TEXT < len(review) <= kb.MAX_TEXT_PROSE  # guards the fixture's premise
    written = kb.append_learnings(path, [{"kind": "review", "text": review}])
    assert written[0]["text"] == review  # whole, untruncated
    assert "[truncated]" not in written[0]["text"]

    # Same text harvested as a terse failure row still gets the default cap.
    terse = kb.append_learnings(path, [{"kind": "failure", "text": review}])
    assert len(terse[0]["text"]) <= kb.MAX_TEXT
    assert terse[0]["text"].endswith(" … [truncated]")


def test_truncation_prefers_a_sentence_boundary() -> None:
    """Over the cap, the cut lands after a sentence terminator — not mid-token."""
    tail = " The final clause that must be cut away entirely." * 20
    text = "First sentence stands. Second sentence also stands." + tail
    out = kb.bound_text(text, "failure")
    assert len(out) <= kb.MAX_TEXT
    body = out[: -len(" … [truncated]")]
    assert out.endswith(" … [truncated]")
    assert body.endswith(".")  # a whole sentence, not a fragment
    assert text.startswith(body)  # nothing invented, only cut


def test_truncation_falls_back_to_a_word_boundary_then_a_hard_cut() -> None:
    """No sentence end in range → cut on whitespace; no whitespace either → hard cut."""
    words = kb.bound_text("word " * 400, "failure")
    assert len(words) <= kb.MAX_TEXT
    assert words.endswith("word … [truncated]")  # a whole word, never "wo"

    unbroken = kb.bound_text("x" * 900, "failure")
    assert len(unbroken) <= kb.MAX_TEXT
    assert unbroken.endswith(" … [truncated]")
    assert set(unbroken[: -len(" … [truncated]")]) == {"x"}


def test_a_boundary_is_ignored_when_it_would_discard_most_of_the_budget() -> None:
    """An early-only sentence end must not shrink the row to a stub — the hard cut wins."""
    text = "Tiny. " + "z" * 900
    out = kb.bound_text(text, "failure")
    assert len(out) > int((kb.MAX_TEXT - len(" … [truncated]")) * 0.6)
    assert out.startswith("Tiny. zzz")


def test_bounded_text_never_exceeds_its_cap_for_any_kind() -> None:
    """Invariant across every valid kind, including the prose ones."""
    long_prose = ("A sentence of moderate length that keeps going on. " * 200).strip()
    for kind in sorted(kb.VALID_KINDS):
        out = kb.bound_text(long_prose, kind)
        assert len(out) <= kb.text_cap(kind), kind
        assert out.endswith(" … [truncated]"), kind


def test_process_retrospective_prose_gets_the_larger_cap(tmp_path) -> None:
    """A REVIEW retrospective is prose too, so it rides the same cap through harvest."""
    path = tmp_path / "kb.jsonl"
    detail = ("The stage template asks for a retrospective but never says what a subtractive "
              "lesson looks like, so every run returns an additive one. " * 4).strip()
    written = kb.append_learnings(path, [{"kind": "process", "text": f"Retro title: {detail}"}])
    assert kb.MAX_TEXT < len(written[0]["text"]) <= kb.MAX_TEXT_PROSE


def test_prose_recall_stays_within_the_context_ceiling(tmp_path) -> None:
    """The larger cap must not push a full recall past the context plane's ceiling."""
    path = tmp_path / "kb.jsonl"
    filler = "The reviewer explained the defect in careful prose about widgets. " * 40
    for i in range(8):
        kb.append_learnings(path, [{"kind": "review", "text": f"widgets case {i}. {filler}"}])
    hits = kb.relevant_learnings(path, {"title_tokens": ["widgets", "case"]}, limit=5)
    assert len(hits) == 5
    assert all(len(h) <= kb.MAX_TEXT_PROSE for h in hits)
    assert _context_bytes({"prior_learnings": hits}) < _MAX_CONTEXT_BYTES


# --- resolved review findings age out of recall (#393) ------------------------------


def _review_task(task_id: str, state: TaskState, text: str) -> Task:
    task = Task(task_id=task_id, run_id="r1", created_at="x", updated_at="x")
    task.state = state
    task.learnings = [text]
    return task


def test_harvest_stamps_the_tasks_terminal_outcome(tmp_path) -> None:
    """Harvest runs at finalize, so the outcome is a fact by the time the row is written."""
    path = tmp_path / "kb.jsonl"
    done = kb.harvest_from_task(
        path, _review_task("t1", TaskState.COMPLETED, "review rejected (cycle 1): leaky lock"), "r1"
    )
    failed = kb.harvest_from_task(
        path, _review_task("t2", TaskState.FAILED, "review rejected (cycle 1): bad guard"), "r1"
    )
    assert done[0]["task_outcome"] == "completed"
    assert failed[0]["task_outcome"] == "failed"
    assert kb.resolved_defect(done[0]) is True
    assert kb.resolved_defect(failed[0]) is False


def test_only_review_findings_can_be_resolved(tmp_path) -> None:
    """A failure/infra/salvage lesson generalizes past the instance that produced it, so
    completing the task does not make it stale the way a fixed rejection does."""
    path = tmp_path / "kb.jsonl"
    task = Task(task_id="t1", run_id="r1", created_at="x", updated_at="x")
    task.state = TaskState.COMPLETED
    task.learnings = [
        "review rejected (cycle 1): the widget lock is re-entrant",
        "implement (attempt 0): the widget suite needs a live socket [infra]",
    ]
    review, failure = kb.harvest_from_task(path, task, "r1")
    assert kb.resolved_defect(review) is True
    assert kb.resolved_defect(failure) is False


def test_a_resolved_finding_loses_to_a_live_one_on_equal_signal(tmp_path) -> None:
    """The #393 demotion: same file overlap, so the still-live lesson takes the top slot
    even though the resolved one is newer (which would win the old recency tiebreak)."""
    path = tmp_path / "kb.jsonl"
    common = {"kind": "review", "files": ["src/pkg/mod.py"], "stage": "review"}
    kb.append_learnings(path, [
        {**common, "text": "live widget finding in src/pkg/mod.py",
         "task_outcome": "failed", "ts": "2026-07-01T00:00:00+00:00"},
        {**common, "text": "fixed widget finding in src/pkg/mod.py",
         "task_outcome": "completed", "ts": "2026-07-09T00:00:00+00:00"},
    ])
    hits = kb.relevant_learnings(path, {"files": ["src/pkg/mod.py"], "title_tokens": ["widget"]})
    assert hits == ["live widget finding in src/pkg/mod.py",
                    "fixed widget finding in src/pkg/mod.py"]


def test_a_resolved_finding_is_demoted_not_excluded(tmp_path) -> None:
    """Unlike a capacity notice or a process row, it still names a real hazard in a file —
    so when it is the only match it keeps its slot rather than dropping out of recall."""
    path = tmp_path / "kb.jsonl"
    kb.append_learnings(path, [{
        "kind": "review", "text": "fixed widget finding in src/pkg/mod.py",
        "files": ["src/pkg/mod.py"], "task_outcome": "completed",
    }])
    hits = kb.relevant_learnings(path, {"files": ["src/pkg/mod.py"]})
    assert hits == ["fixed widget finding in src/pkg/mod.py"]


def test_demotion_still_ranks_a_resolved_file_hit_over_a_weaker_live_match(tmp_path) -> None:
    """The bit sits BELOW file-overlap, so it reorders same-file entries without letting a
    live stage-only match leapfrog a resolved finding about the file actually in play."""
    path = tmp_path / "kb.jsonl"
    kb.append_learnings(path, [
        {"kind": "review", "text": "fixed finding in src/pkg/mod.py",
         "files": ["src/pkg/mod.py"], "task_outcome": "completed"},
        {"kind": "failure", "text": "unrelated stage note", "stage": "review"},
    ])
    hits = kb.relevant_learnings(path, {"files": ["src/pkg/mod.py"], "stage": "review"})
    assert hits == ["fixed finding in src/pkg/mod.py", "unrelated stage note"]


def test_a_legacy_row_without_an_outcome_counts_as_unresolved(tmp_path) -> None:
    """Every row predating #393 lacks the stamp; unknown must not read as fixed, or adding
    the field would silently demote the whole existing KB."""
    path = tmp_path / "kb.jsonl"
    kb.append_learnings(path, [
        {"kind": "review", "text": "legacy widget finding", "files": ["src/pkg/mod.py"],
         "ts": "2026-07-01T00:00:00+00:00"},
        {"kind": "review", "text": "fixed widget finding", "files": ["src/pkg/mod.py"],
         "task_outcome": "completed", "ts": "2026-07-09T00:00:00+00:00"},
    ])
    stored = kb.read_entries(path)
    assert "task_outcome" not in stored[0]  # absent, not null
    assert kb.resolved_defect(stored[0]) is False
    hits = kb.relevant_learnings(path, {"files": ["src/pkg/mod.py"]})
    assert hits == ["legacy widget finding", "fixed widget finding"]


def test_resolution_bit_does_not_weaken_the_no_signal_floor(tmp_path) -> None:
    """The guard on the #393 splice: the demotion lives in the sort key, never in _score's
    signal tuple, so `sum(score) == 0` still means 'matched nothing' for every entry."""
    path = tmp_path / "kb.jsonl"
    kb.append_learnings(path, [
        {"kind": "review", "text": "utterly unrelated content here", "task_outcome": "failed"},
        {"kind": "review", "text": "utterly unrelated content too", "task_outcome": "completed"},
    ])
    assert kb.relevant_learnings(path, {"title_tokens": ["something", "else"]}) == []
    for entry in kb.read_entries(path):
        assert len(kb._score(entry, {"title_tokens": ["nope"]})) == 4


# --- #480: append-only prune (amendments) + task_outcome backfill ---------------------


def _one(path, text="review nit about src/pkg/cli.py", **kw) -> dict:
    """Append one lesson (files auto-derived from its own text) and hand back the row."""
    files = kw.pop("files", kb.mentioned_files(text, ["src/pkg/auth.py", "src/pkg/a.py"]))
    entry = {"kind": "review", "text": text, "files": files, **kw}
    return kb.append_learnings(path, [entry])[0]


def test_amendment_fold_retires_stamps_and_last_write_wins(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    entry = _one(path)
    kb.append_amendments(path, [
        {"amends": entry["id"], "task_outcome": "failed"},
        {"amends": entry["id"], "task_outcome": "completed"},  # later record wins
        {"amends": entry["id"], "retired": True, "reason": "describes code that is gone"},
    ])

    assert kb.read_entries(path) == []  # retired rows leave the live pool
    folded = kb.read_entries(path, include_retired=True)
    assert len(folded) == 1
    assert folded[0]["task_outcome"] == "completed"
    assert folded[0]["retired"] is True
    assert folded[0]["retired_reason"] == "describes code that is gone"
    # the ORIGINAL line survives untouched — the fold never rewrites the log
    raw = kb.read_records(path)
    assert len(raw) == 4 and raw[0] == entry
    assert [r["kind"] for r in raw[1:]] == ["amendment"] * 3


def test_amendments_are_never_lessons(tmp_path) -> None:
    """An amendment must not surface as advice, and one naming an unknown id invents
    nothing — it stays in the raw log as the record of a decision."""
    path = tmp_path / "kb.jsonl"
    _one(path, text="watch the auth cache in src/pkg/auth.py")
    kb.append_amendments(path, [{"amends": "lk-doesnotexist", "retired": True}])

    assert len(kb.read_entries(path, include_retired=True)) == 1
    assert len(kb.read_records(path)) == 2
    texts = kb.relevant_learnings(
        path, {"files": ["src/pkg/auth.py"], "stage": None, "failure_kind": None,
               "title_tokens": []},
    )
    assert texts == ["watch the auth cache in src/pkg/auth.py"]


def test_retired_row_leaves_recall(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    stale = _one(path, text="stale finding in src/pkg/auth.py")
    live = _one(path, text="live hazard in src/pkg/auth.py")
    query = {"files": ["src/pkg/auth.py"], "stage": None, "failure_kind": None,
             "title_tokens": []}
    assert set(kb.relevant_learnings(path, query)) == {stale["text"], live["text"]}

    kb.append_amendments(path, [{"amends": stale["id"], "retired": True}])
    assert kb.relevant_learnings(path, query) == [live["text"]]


def test_retired_row_still_suppresses_a_reappend(tmp_path) -> None:
    """Retiring is a judgement that a lesson is stale; the next harvest of the same text
    must not resurrect it, or the prune would undo itself on the following run."""
    path = tmp_path / "kb.jsonl"
    entry = _one(path, text="the same lesson, learned again")
    kb.append_amendments(path, [{"amends": entry["id"], "retired": True}])

    assert kb.append_learnings(path, [{"kind": "review", "text": "the same lesson, learned again"}]) == []
    assert kb.read_entries(path) == []


def test_append_amendments_skips_records_that_assert_nothing(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    entry = _one(path)
    written = kb.append_amendments(path, [
        {"retired": True},                                     # no target
        {"amends": entry["id"]},                               # no change
        {"amends": entry["id"], "task_outcome": "not_a_state"},  # unknown state
    ])
    assert written == []
    assert len(kb.read_records(path)) == 1


def test_select_entries_ands_selectors_and_never_selects_everything(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    old = _one(path, text="old review of src/a.py", run_id="r1", ts="2026-06-01T00:00:00+00:00")
    new = _one(path, text="new review of src/b.py", run_id="r2", ts="2026-08-01T00:00:00+00:00")
    fail = kb.append_learnings(path, [{
        "kind": "failure", "text": "flaky harness", "run_id": "r1",
        "ts": "2026-06-02T00:00:00+00:00",
    }])[0]
    entries = kb.read_entries(path)

    # a bare prune must select NOTHING, not the whole KB
    assert kb.select_entries(entries) == []
    assert [e["id"] for e in kb.select_entries(entries, before="2026-07-01")] == [old["id"], fail["id"]]
    # selectors AND together
    assert [e["id"] for e in kb.select_entries(entries, before="2026-07-01", kinds=["review"])] == [old["id"]]
    assert [e["id"] for e in kb.select_entries(entries, run_id="r2")] == [new["id"]]
    assert [e["id"] for e in kb.select_entries(entries, ids=[fail["id"]])] == [fail["id"]]
    # every row here is an unstamped review or a non-review; none is resolved
    assert [e["id"] for e in kb.select_entries(entries, unstamped=True)] == [old["id"], new["id"]]
    assert kb.select_entries(entries, resolved=True) == []

    kb.append_amendments(path, [{"amends": new["id"], "task_outcome": "completed"}])
    stamped = kb.read_entries(path)
    assert [e["id"] for e in kb.select_entries(stamped, resolved=True)] == [new["id"]]
    assert [e["id"] for e in kb.select_entries(stamped, unstamped=True)] == [old["id"]]


def _task_doc(root, run_id, task_id, state, *, flat=False) -> None:
    import json as _json

    d = root if flat else root / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"status-{run_id}-{task_id}.json").write_text(
        _json.dumps({"run_id": run_id, "task_id": task_id, "state": state}), encoding="utf-8"
    )


def test_outcome_from_run_logs_resolves_only_terminal_states(tmp_path) -> None:
    root = tmp_path / "runs"
    _task_doc(root, "r1", "#12", "completed")
    _task_doc(root, "r2", "#13", "running")        # mid-flight says nothing about a fix
    _task_doc(root, "r3", "#14", "flat", flat=True)  # placeholder, overwritten below
    _task_doc(root, "r3", "#14", "failed", flat=True)

    assert kb.outcome_from_run_logs(root, {"run_id": "r1", "task_id": "#12"}) == "completed"
    assert kb.outcome_from_run_logs(root, {"run_id": "r2", "task_id": "#13"}) is None
    # the flat (non-nested) store layout is the fallback
    assert kb.outcome_from_run_logs(root, {"run_id": "r3", "task_id": "#14"}) == "failed"
    # unknown must never read as resolved: no ids, a pruned run dir, unparseable JSON
    assert kb.outcome_from_run_logs(root, {"run_id": None, "task_id": None}) is None
    assert kb.outcome_from_run_logs(root, {"run_id": "gone", "task_id": "#1"}) is None
    (root / "r1" / "status-r1-#99.json").write_text("{not json", encoding="utf-8")
    assert kb.outcome_from_run_logs(root, {"run_id": "r1", "task_id": "#99"}) is None
    # an id that could climb out of the runs root is refused rather than followed
    assert kb.outcome_from_run_logs(root, {"run_id": "../etc", "task_id": "#1"}) is None


def test_cli_kb_prune_previews_then_retires(tmp_path, capsys) -> None:
    import json

    root = str(tmp_path)
    path = tmp_path / "learnings-kb.jsonl"
    stale = _one(path, text="stale finding in src/pkg/auth.py", run_id="r1")
    _one(path, text="live hazard in src/pkg/auth.py", run_id="r2")

    # dry run reports the selection and writes nothing
    assert cli_main(["--root", root, "kb", "prune", "--run", "r1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["selected"] == 1 and out["retired"] == 0
    assert out["entries"][0]["id"] == stale["id"]
    assert len(kb.read_records(path)) == 2

    assert cli_main(["--root", root, "kb", "prune", "--run", "r1", "--apply",
                     "--reason", "code is gone"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is False and out["retired"] == 1

    assert kb.relevant_learnings(
        path, {"files": ["src/pkg/auth.py"], "stage": None, "failure_kind": None,
               "title_tokens": []},
    ) == ["live hazard in src/pkg/auth.py"]

    assert cli_main(["--root", root, "kb", "show"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1
    assert cli_main(["--root", root, "kb", "show", "--include-retired"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["count"] == 2
    retired = [e for e in shown["entries"] if e.get("retired")]
    assert len(retired) == 1 and retired[0]["retired_reason"] == "code is gone"


def test_cli_kb_backfill_outcomes_stamps_from_run_logs(tmp_path, capsys) -> None:
    import json

    root = str(tmp_path)
    path = tmp_path / "learnings-kb.jsonl"
    resolvable = _one(path, text="a finding in src/pkg/a.py", run_id="r1", task_id="#12")
    orphan = _one(path, text="a finding from a pruned run", run_id="gone", task_id="#99")
    _task_doc(tmp_path, "r1", "#12", "completed")

    assert cli_main(["--root", root, "kb", "backfill-outcomes"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["candidates"] == 2
    assert out["resolved"] == 1 and out["unresolved"] == 1 and out["stamped"] == 0
    assert out["outcomes"] == [{"id": resolvable["id"], "task_outcome": "completed"}]
    assert out["unresolved_entries"][0]["id"] == orphan["id"]
    assert len(kb.read_records(path)) == 2  # preview wrote nothing

    assert cli_main(["--root", root, "kb", "backfill-outcomes", "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["stamped"] == 1

    entries = {e["id"]: e for e in kb.read_entries(path)}
    assert entries[resolvable["id"]]["task_outcome"] == "completed"
    assert kb.resolved_defect(entries[resolvable["id"]]) is True
    # the unresolvable row stays unstamped, so it keeps reading as still-live
    assert "task_outcome" not in entries[orphan["id"]]
    assert kb.resolved_defect(entries[orphan["id"]]) is False
