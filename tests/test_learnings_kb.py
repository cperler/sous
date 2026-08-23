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
from orchestrator.schemas.enums import ResultStatus
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
