"""Engine CLI — the supervisor's Bash entry points (target.md §3).

The supervisor loop is: ``ready`` -> ``next`` (get a WorkItem) -> [the execution
lane runs it] -> ``record`` (ingest the StageResult) -> repeat. The CLI speaks JSON
on stdout so a Bash/skill supervisor can drive it. It never calls a model.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from adapters.execution.runners import build_registry
from adapters.project.base import ADAPTER_CONTRACT_VERSION

from .cost_ledger import CostLedger
from .engine import Engine
from .project_loader import load_project, validate_config
from .routing import Router
from .schemas.enums import ExecutionLane, ExecutionMode, Provider
from .schemas.work import StageResult


def _is_shared_runs_root(root: Path, run: str) -> bool:
    """True when ``root`` already holds other runs' store data — i.e. it is a
    *shared* runs-root (the parent the #6 dashboard scans), not this run's own
    per-run store dir. Detected structurally (no name heuristics): a
    ``learnings-kb.jsonl`` living here, a child directory that is itself a run
    store, or a flat ``status-*.json`` doc belonging to a different run."""
    if not root.is_dir():
        return False
    if (root / "learnings-kb.jsonl").exists():
        return True
    for child in root.iterdir():
        if child.is_dir() and any(child.glob("status-*.json")):
            return True  # a nested per-run store already lives under root
        # A flat status-<other>.json / status-<other>-<task>.json belongs to a
        # comingled sibling run (this run's own docs are prefixed by `status-<run>`).
        if (
            child.is_file()
            and child.name.startswith("status-")
            and child.suffix == ".json"
            and child.name != f"status-{run}.json"
            and not child.name.startswith(f"status-{run}-")
        ):
            return True
    return False


def _resolve_store_root(root: Path, run: str | None) -> Path:
    """Resolve the actual per-run store directory from ``--root`` (#81).

    ``--root`` is ambiguous: dashboard/kb/tail treat it as the runs-root (parent
    of the per-run dirs), while the per-run commands historically treat it as the
    run's own store dir. Typing the dashboard spelling (``--root runs --run <id>``)
    for ``init-run`` therefore wrote the whole store flat into ``runs/`` and pushed
    the learnings KB (``<store parent>/learnings-kb.jsonl``) up to the repo root.

    To make ``--root runs`` the natural spelling everywhere, auto-nest to
    ``<root>/<run>/`` when ``<root>`` is a shared runs-root. The decision is
    anchored to where this run's store already lives so it stays stable across a
    run's init-run/add-task/next/record calls (a KB file materializing mid-run must
    not flip a flat run into a nested one): if the run's own flat run-doc already
    sits in ``<root>`` we keep it there; if ``<root>/<run>/`` already exists we use
    that; only a fresh run under a shared root nests. Otherwise use ``<root>``
    directly, preserving callers that already point at the per-run dir."""
    if not run:
        return root
    if (root / f"status-{run}.json").exists():
        return root  # this run is already established flat here — stay put
    nested = root / run
    if nested.is_dir() or _is_shared_runs_root(root, run):
        return nested
    return root


def _engine(args: argparse.Namespace) -> Engine:
    from .status_store import StatusStore

    root = _resolve_store_root(Path(args.root), getattr(args, "run", None))
    root.mkdir(parents=True, exist_ok=True)
    if str(root) != str(args.root):
        # Never silent: state the nesting so the operator sees where the store landed.
        print(
            f"note: --root {args.root!s} is a shared runs-root; nesting this run's "
            f"store at {root!s} (learnings KB stays at {root.parent!s}/learnings-kb.jsonl)",
            file=sys.stderr,
        )
    store = StatusStore(root)
    ledger = CostLedger(root / "stage-costs.jsonl")
    project = load_project(args.project)

    # Config-only execution mode (interactive×claude default; headless = in-process).
    mode = ExecutionMode(getattr(args, "mode", "interactive"))
    if args.cmd == "run-headless":
        mode = ExecutionMode.HEADLESS  # this command drives in-process; force the lane
    provider = Provider(args.provider) if getattr(args, "provider", None) else None
    interactive = mode is ExecutionMode.INTERACTIVE and provider is not Provider.CODEX
    # Codex full-validation AND the headless×claude --json-schema both need the project's
    # stage schemas (optional hook); wire the same provider into both lanes.
    schema_provider = getattr(project, "schema_for", None)
    registry = build_registry(
        include_interactive=interactive,
        headless_schema_provider=schema_provider,
        codex_schema_provider=schema_provider,
        setup_project=project,  # wires the deterministic ENGINE-lane intake runner
        run_log_root=root,  # #56: tee each provider call's raw stdout/stderr under stages/
    )
    router = Router(execution_mode=mode, orchestrator_provider=provider)
    return Engine(store, ledger, project, router=router, registry=registry)


def _emit(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator")
    p.add_argument("--root",
                   help="runs-root or per-run store dir (not needed for validate). Per-run "
                        "commands auto-nest under <root>/<run>/ when <root> is a shared "
                        "runs-root (holds a learnings-kb.jsonl or other runs' stores), so "
                        "--root runs is the natural spelling shared with dashboard/kb/tail")
    p.add_argument("--run", help="run id (not needed for validate)")
    p.add_argument("--project",
                   help="project-config module (e.g. adapters.project.heysoo) or a "
                        "project-owned adapter dir (e.g. ../my-project/.orchestration)")
    p.add_argument("--mode", default="interactive", choices=["interactive", "headless"],
                   help="execution mode (config-only lane selection)")
    p.add_argument("--provider", default=None, choices=["claude", "codex"],
                   help="global provider override (ORCHESTRATOR_PROVIDER)")
    sub = p.add_subparsers(dest="cmd", required=True)

    ir = sub.add_parser("init-run")
    ir.add_argument("--lane", default="full")
    ir.add_argument("--budget-usd", type=float, default=None,
                    help="per-run metered-spend budget in USD (#34): a soft warning at "
                         "80%%, a hard PAUSE at/after the budget")
    ir.add_argument("--route-by-cost", action="store_true",
                    help="enable cost-aware lane routing: un-pinned tasks get a cheaper "
                         "lane preset as the remaining budget thins")
    ir.add_argument("--route-by-capacity", action="store_true",
                    help="enable capacity-aware model downgrade (#12): a FRESH dispatch "
                         "drops to a cheaper model while utilization is high (>=70%%, below "
                         "the 90%% wait gate) so work keeps progressing under load")
    ir.add_argument("--cross-provider-fallback", action="store_true",
                    help="enable codex→claude fallthrough (#7): when a codex stage's "
                         "same-provider options are exhausted (floor rate-limit, waits spent) "
                         "or codex is unavailable (CLI missing / auth expired), re-route its "
                         "next dispatch to the equivalent claude lane instead of failing. "
                         "One-way, once per stage; the flag is blanket consent (even a "
                         ":codex-pinned task falls through). Default off")
    ir.add_argument("--warm-retry", action="store_true",
                    help="reuse a failed attempt's session on the retry (#8) when the failure "
                         "was mechanical (timeout / rate-limit / infra), same-provider, and the "
                         "worktree still matches the session (salvage kept the work, or a "
                         "non-git stage). A content failure (schema violation, real test "
                         "failure, review rejection) always retries cold. Default off (the "
                         "design-pass §2 fresh-after-failure default) — this is the opt-in")
    ir.add_argument("--progress-comments", action="store_true",
                    help="post mid-run progress commentary to the driving issue/PR (#64): "
                         "an upserted living comment/PR-body section at each stage boundary "
                         "(default off — outward-facing, opt-in for real-repo runs)")
    at = sub.add_parser("add-task")
    at.add_argument("--task", required=True)
    at.add_argument("--pipeline", default=None,
                    help="comma-separated stage list (e.g. 'intake,implement,review'); "
                         "default: the run lane's preset")
    at.add_argument("--depends-on", default=None,
                    help="comma-separated task ids this task depends on (DAG edges for "
                         "the batch scheduler; overrides the task source)")
    at.add_argument("--provider-tag", default=None, choices=["claude", "codex"],
                    help="per-task provider routing tag (the old '82:codex' tag)")
    at.add_argument("--deterministic-stages", default=None,
                    help="comma-separated stages to run on the $0 ENGINE lane instead of a "
                         "model (e.g. 'test,deliver'); intake is always deterministic (#33)")
    at.add_argument("--estimate", default=None,
                    help="rough size hint (small/medium/large or a USD number) — feeds "
                         "cost-aware lane routing on a route-by-cost run (#34)")
    at.add_argument("--model", default=None,
                    help="per-task model pin: a friendly alias (fable/opus/sonnet/haiku) or an "
                         "exact table id (e.g. gpt-5.5). Overrides the role default on model-lane "
                         "stages so a heavy-architecture task runs on a higher tier, e.g. "
                         "'--model fable' (claude-fable-5). Validated against the task's provider "
                         "at add time (#84)")
    util_help = "5h utilization %% for the capacity gates: a number, or 'auto' to probe"
    n = sub.add_parser("next")
    n.add_argument("--task", required=True)
    n.add_argument("--util", default="0", help=util_help)
    n.add_argument("--resume", action="store_true",
                   help="re-emit the pending WorkItem for a task whose supervisor crashed "
                        "holding the dispatch lease (bypasses the lease/cooldown guard so a "
                        "crashed supervisor recovers its item without hand-editing state; #50)")
    sub.add_parser("record").add_argument("--result", required=True, help="StageResult JSON file")
    d = sub.add_parser("dispatchable")
    d.add_argument("--util", default="0", help=util_help)
    d.add_argument("--max-concurrent", type=int, default=3)
    rh = sub.add_parser("run-headless", help="drive the whole run in-process (headless mode)")
    rh.add_argument("--util", default="0", help=util_help)
    rh.add_argument("--max-concurrent", type=int, default=3)
    rh.add_argument("--wait", action="store_true",
                    help="sleep through capacity stalls / rate-limit cooldowns instead of "
                         "returning (the old capacity_wait_loop)")
    sub.add_parser("util", help="probe the account's 5h/7d utilization (feeds --util)")
    sub.add_parser("statusline",
                   help="one-line 5h/7d utilization for the Claude Code status bar "
                        "(reads the same usage cache as util; quiet on a probe miss)")
    hd = sub.add_parser("hold", help="park a task at the human approval gate")
    hd.add_argument("--task", required=True)
    hd.add_argument("--reason", required=True, help="what needs human sign-off")
    ap = sub.add_parser("approve", help="release a held task (writes the approval artifact)")
    ap.add_argument("--task", required=True)
    ap.add_argument("--by", required=True, help="who is approving")
    ap.add_argument("--note", default="", help="what is being approved")
    up = sub.add_parser("unpause", help="release a PAUSED run (e.g. after the batch circuit "
                                        "breaker tripped and the systemic cause is fixed)")
    up.add_argument("--raise-budget", type=float, default=None,
                    help="on a budget-exhausted pause (#34): resume with this NEW budget "
                         "ceiling (re-arms the soft warning). Omit to drop the cap entirely")
    rj = sub.add_parser("reject", help="confirm-and-close a held infeasible task (writes the rejection artifact)")
    rj.add_argument("--task", required=True)
    rj.add_argument("--by", required=True, help="who is rejecting")
    rj.add_argument("--reason", required=True, help="why the task is infeasible")
    sub.add_parser("resume")
    sub.add_parser("status")
    wt = sub.add_parser("watch", help="poll a run to terminal, alerting (project notify "
                                      "hook) on stalls and terminal states — works for "
                                      "any run, incl. single-task engine-lane runs")
    wt.add_argument("--interval", type=int, default=60, help="poll interval seconds")
    wt.add_argument("--stale-after", type=int, default=1800,
                    help="a task with no update for this many seconds is flagged stale")
    wt.add_argument("--activity", action="store_true",
                    help="also print a live activity line per running task (#66): what the "
                         "model is doing + seconds since its stream last grew")
    wt.add_argument("--stall-after", type=int, default=300,
                    help="with --activity: a stream that hasn't grown for this many seconds "
                         "while its stage is RUNNING gets a distinct STREAM STALLED note")
    tl = sub.add_parser("tail", help="print the recent tail of a task's current (or last) "
                                     "headless stream (#66); --follow to poll for new output")
    tl.add_argument("task", help="task id whose stream to tail")
    tl.add_argument("--stage", default=None,
                    help="a specific stage's stream (default: the most-recent stream)")
    tl.add_argument("--lines", type=int, default=20, help="how many trailing lines to show")
    tl.add_argument("--follow", action="store_true", help="poll for and print new output")
    tl.add_argument("--interval", type=int, default=2, help="--follow poll interval seconds")
    sub.add_parser("cost-report", help="per-stage/-task cost breakdown + the session-reuse win")
    sub.add_parser("retrospective", help="failure retrospective (patterns + what the retries learned)")
    sub.add_parser("validate", help="check a project adapter against the engine's contract (no run needed)")
    sp = sub.add_parser("spec", help="front door (#18): idea → validated spec → dependency-ordered issues")
    spsub = sp.add_subparsers(dest="spec_cmd", required=True)
    spv = spsub.add_parser("validate", help="schema + DAG check a spec file (no writes)")
    spv.add_argument("file", help="spec JSON file")
    spp = spsub.add_parser("plan", help="print the ordered filing plan (no writes)")
    spp.add_argument("file", help="spec JSON file")
    spp.add_argument("--budget-usd", type=float, default=None,
                     help="a-priori check (#34): print the tasks' estimated total vs this "
                          "budget from their `estimate` hints (advisory)")
    spp.add_argument("--strict", action="store_true",
                     help="with --budget-usd: exit non-zero if the estimate overruns")
    spf = spsub.add_parser("file", help="file each task as an issue in dependency order")
    spf.add_argument("file", help="spec JSON file")
    spf.add_argument("--dry-run", action="store_true",
                     help="print exactly what would be created; file nothing")
    spf.add_argument("--budget-usd", type=float, default=None,
                     help="a-priori check (#34): with --strict, refuse to file if the "
                          "tasks' estimated total overruns this budget")
    spf.add_argument("--strict", action="store_true",
                     help="with --budget-usd: refuse to file (exit non-zero) on overrun")
    spf.add_argument("--archive-dir", default="./specs",
                     help="dir to archive the filed spec + local-id→issue-ref mapping into, "
                          "as <slug>.json, so the conformance gate can find it later "
                          "(default ./specs/; skipped on --dry-run)")
    # Accept --project after the subcommand too (SUPPRESS: don't clobber the global one).
    spf.add_argument("--project", default=argparse.SUPPRESS,
                     help="project-config module/dir supplying the task source (may also precede 'spec')")
    spc = spsub.add_parser("conformance",
                           help="whole-spec acceptance gate (#18): checklist of each spec "
                                "task's filed issue, state, PR + acceptance criteria; exit 1 "
                                "if any issue is still open")
    spc.add_argument("file", help="spec JSON file (ideally an archived <slug>.json)")
    spc.add_argument("--json", action="store_true", help="emit the checklist as JSON")
    spc.add_argument("--project", default=argparse.SUPPRESS,
                     help="project-config module/dir supplying the task source used to look "
                          "up issue state + PRs (may also precede 'spec')")
    bp = sub.add_parser("batch-plan", help="producer (#57): auto-analysis DAG over an "
                                           "ALREADY-FILED batch of issues (skill authors the "
                                           "plan; this validates/applies it)")
    bpsub = bp.add_subparsers(dest="batch_cmd", required=True)
    bpc = bpsub.add_parser("candidates", help="list open issues as the model's analysis "
                                              "input (JSON: id/title/body-excerpt/labels/deps)")
    bpc.add_argument("--label", default=None, help="only issues with this label")
    bpc.add_argument("--limit", type=int, default=50, help="max issues to list")
    bpc.add_argument("--project", default=argparse.SUPPRESS,
                     help="project-config module/dir supplying the task source (may also precede 'batch-plan')")
    bpv = bpsub.add_parser("validate", help="schema + DAG check a plan file (cycles/dups/"
                                            "unknown refs). With --project, verifies external "
                                            "edges against the repo's issues")
    bpv.add_argument("file", help="batch plan JSON file")
    bpv.add_argument("--project", default=argparse.SUPPRESS,
                     help="verify external edges against this project's open issues")
    bpa = bpsub.add_parser("apply", help="add the plan's tasks to a run in topological order")
    bpa.add_argument("file", help="batch plan JSON file")
    bpa.add_argument("--dry-run", action="store_true",
                     help="print exactly what would be added; add nothing")
    bpa.add_argument("--project", default=argparse.SUPPRESS,
                     help="project-config module/dir supplying the task source (may also precede 'batch-plan')")
    bs = sub.add_parser("brainstorm", help="front door above intake (#2): a fuzzy area → "
                                           "ranked shortlist of ideas → filed enhancement "
                                           "issues (skill authors the ideas; this ranks/files)")
    bssub = bs.add_subparsers(dest="brainstorm_cmd", required=True)
    bsv = bssub.add_parser("validate", help="schema-check a brainstorm session file (no writes)")
    bsv.add_argument("file", help="brainstorm JSON file")
    bsc = bssub.add_parser("capture", help="print the ranked shortlist; --file-selected files "
                                           "the chosen ideas as issues")
    bsc.add_argument("file", help="brainstorm JSON file")
    bsc.add_argument("--file-selected", default=None,
                     help="comma-separated 1-based shortlist ranks to file as issues "
                          "(e.g. 1,3); omit to only print the shortlist")
    bsc.add_argument("--dry-run", action="store_true",
                     help="with --file-selected: print exactly what would be filed; file nothing")
    bsc.add_argument("--project", default=argparse.SUPPRESS,
                     help="project-config module/dir supplying the task source (may also "
                          "precede 'brainstorm')")
    gc = sub.add_parser("gc", help="list/prune long-lived git checkpoint tags (no run/project needed)")
    gc.add_argument("--repo", default=".", help="git repo/worktree to scan for checkpoint tags")
    gc.add_argument("--keep-latest", type=int, default=0,
                    help="keep the N newest matching tags; the rest are prune candidates")
    gc.add_argument("--prune", action="store_true",
                    help="actually delete the candidate tags (default is a dry-run preview)")
    # SUPPRESS: only override the global --run when explicitly given here, so
    # `--run R gc` (global position) is not clobbered by a subparser default.
    gc.add_argument("--run", default=argparse.SUPPRESS,
                    help="scope to one run's checkpoint tags (may also precede the subcommand)")

    db = sub.add_parser("dashboard", help="cross-session board (#6): one attention-first view "
                                          "over ALL runs under --root (runs/), not just one")
    db.add_argument("--watch", action="store_true", help="clear-screen + reprint on a loop")
    db.add_argument("--interval", type=int, default=30, help="--watch refresh interval seconds")
    db.add_argument("--limit", type=int, default=20, help="max runs to show")
    db.add_argument("--all", action="store_true",
                    help="show every run (default: non-terminal + the 5 most-recent terminal)")
    db.add_argument("--stale-after", type=int, default=1800,
                    help="a task with no update for this many seconds is flagged stale")

    kb = sub.add_parser("kb", help="cross-run learnings KB (#72): show relevant prior "
                                   "learnings, or teach the system a lesson (--root = runs/)")
    kbsub = kb.add_subparsers(dest="kb_cmd", required=True)
    kbs = kbsub.add_parser("show", help="print KB entries, most-relevant-first when --query given")
    kbs.add_argument("--query", help="space-separated tokens to score entries against")
    kbs.add_argument("--limit", type=int, default=20, help="max entries to show")
    kba = kbsub.add_parser("add", help="append a manual learning (the human teaching the system)")
    kba.add_argument("text", help="the lesson text (bounded to ~500 chars)")
    kba.add_argument("--kind", default="manual",
                     help="failure|review|infra|salvage|manual (default manual)")
    kba.add_argument("--stage", help="optional stage this lesson is about")
    kba.add_argument("--files", help="optional comma-separated files this lesson touches")

    args = p.parse_args(argv)

    if args.cmd == "kb":
        # The cross-run learnings KB (#72). --root is the runs-root (parent of the run dirs,
        # same as dashboard); the KB lives at <runs-root>/learnings-kb.jsonl unless a project
        # override / env var relocates it. --project is optional (only for that override).
        from .learnings_kb import (
            append_learnings,
            read_entries,
            relevant_learnings,
            resolve_kb_path,
            tokenize,
        )

        if not args.root:
            p.error("--root is required for kb (the runs-root, e.g. runs/)")
        project = load_project(args.project) if args.project else None
        path = resolve_kb_path(Path(args.root), project)
        if args.kb_cmd == "add":
            files = [f.strip() for f in (args.files or "").split(",") if f.strip()]
            written = append_learnings(path, [{
                "kind": args.kind, "text": args.text, "stage": args.stage,
                "files": files, "run_id": None, "task_id": None,
            }])
            _emit({"ok": True, "path": str(path), "added": len(written),
                   "entry": written[0] if written else None})
            return 0
        # show
        if args.query:
            tokens = tokenize(args.query)
            texts = relevant_learnings(
                path, {"files": [], "stage": None, "failure_kind": None,
                       "title_tokens": tokens}, limit=args.limit,
            )
            _emit({"path": str(path), "query": args.query, "count": len(texts),
                   "learnings": texts})
        else:
            entries = read_entries(path)[-args.limit:]
            _emit({"path": str(path), "count": len(entries), "entries": entries})
        return 0

    if args.cmd == "gc":
        # Checkpoint tags (task/<run>/<task>/<stage>/<attempt>) outlive their run; list
        # them newest-first, hold back --keep-latest N, and delete the rest under --prune.
        from adapters.execution.transport import _git

        from .engine import _ref_safe

        scope = f"task/{_ref_safe(args.run)}/*" if args.run else "task/*"
        listing = _git(args.repo, "tag", "-l", "--sort=-creatordate", scope)
        tags = [t for t in listing.stdout.splitlines() if t.strip()]
        kept = tags[:args.keep_latest] if args.keep_latest else []
        doomed = tags[args.keep_latest:] if args.keep_latest else tags
        deleted = []
        if args.prune:
            for tag in doomed:
                if _git(args.repo, "tag", "-d", tag).returncode == 0:
                    deleted.append(tag)
        _emit({"repo": args.repo, "scope": scope, "kept": kept, "candidates": doomed,
               "deleted": deleted, "dry_run": not args.prune})
        return 0

    if args.cmd == "tail":
        # In-flight stream tail (#66): reads files under --root only — no engine/project/model.
        # An interactive/ENGINE-lane task (or one that hasn't dispatched yet) has no stream;
        # say so cleanly rather than erroring.
        from .stream_probe import find_current_stream, follow_stream, read_tail

        if not args.root:
            p.error("--root is required for tail")
        path = find_current_stream(Path(args.root), args.task, args.stage)
        if path is None:
            print(f"(no live stream for task {args.task} — interactive/ENGINE lane, "
                  "or nothing dispatched yet)")
            return 0
        if args.follow:
            import contextlib
            import time

            with contextlib.suppress(KeyboardInterrupt):  # interactive Ctrl-C ends the follow
                follow_stream(path, emit=print, sleeper=time.sleep,
                              lines=args.lines, poll_interval=args.interval)
            return 0
        for line in read_tail(path, lines=args.lines) or []:
            print(line)
        return 0

    if args.cmd == "dashboard":
        # Cross-session board (#6): reads every runs/<id>/ store under --root. Needs --project
        # to build the per-run read-only engine (like `status`), but NOT --run (it spans runs).
        from .dashboard import (
            dashboard_snapshot,
            default_engine_factory,
            render_dashboard,
            render_watch,
        )
        from .usage_probe import read_usage

        if not args.root or not args.project:
            p.error("--root and --project are required for dashboard")
        factory = default_engine_factory(args.project, mode=args.mode, provider=args.provider)
        snap_kw = dict(
            stale_after_s=args.stale_after, limit=args.limit, show_all=args.all,
            engine_factory=factory, usage_reader=read_usage,
        )
        if args.watch:
            import contextlib
            import time

            # Ctrl-C ends the loop cleanly (render_watch also swallows KeyboardInterrupt).
            with contextlib.suppress(KeyboardInterrupt):
                render_watch(args.root, emit=print, sleeper=time.sleep,
                             interval=args.interval, **snap_kw)
            return 0
        print(render_dashboard(dashboard_snapshot(args.root, **snap_kw)))
        return 0

    if args.cmd == "util":
        # The capacity sensor (needs no run/project): probe the usage endpoint and emit
        # the numbers the --util gates consume. A probe miss is an explicit field, not
        # an error — callers fall back to 0.0 (gates open) knowingly.
        from dataclasses import asdict

        from .usage_probe import read_usage

        usage = read_usage()
        _emit({"available": usage is not None, **(asdict(usage) if usage else {})})
        return 0

    if args.cmd == "statusline":
        # Display-only sibling of `util`: same cache, but a raw line for Claude Code's
        # statusLine (which consumes plain text, not JSON). Quiet on a probe miss so the
        # status bar shows nothing rather than an error. Always exit 0.
        from .usage_probe import format_statusline, read_usage

        line = format_statusline(read_usage())
        if line:
            print(line)
        return 0

    if args.cmd == "validate":
        # Loading an external dir already enforces CONTRACT_VERSION + the full surface;
        # validate_config additionally reports on module-path adapters.
        config = load_project(args.project)
        missing = validate_config(config)
        _emit({"project": getattr(config, "name", None), "valid": not missing,
               "missing": missing, "contract_version": ADAPTER_CONTRACT_VERSION})
        return 1 if missing else 0

    if args.cmd == "spec":
        # The front door (#18). validate/plan are pure (no project); file needs a task
        # source. load_spec raises SpecError (a clear message) on any bad input.
        from .spec_intake import (
            SpecError,
            estimate_budget,
            file_spec,
            load_spec,
            render_estimate,
            topological_order,
        )
        from .spec_intake import plan as spec_plan

        try:
            spec = load_spec(args.file)
        except SpecError as exc:
            _emit({"ok": False, "error": str(exc)})
            return 1
        if args.spec_cmd == "validate":
            _emit({"ok": True, "title": spec["title"], "tasks": len(spec["tasks"]),
                   "order": topological_order(spec)})
            return 0
        if args.spec_cmd == "plan":
            sys.stdout.write(spec_plan(spec) + "\n")
            # A-priori cost estimate (#34): only when a budget is given (advisory math).
            if args.budget_usd is not None:
                est = estimate_budget(spec, budget_usd=args.budget_usd)
                sys.stdout.write("\n" + render_estimate(est) + "\n")
                if est["overrun"] and args.strict:
                    return 1
            return 0
        if args.spec_cmd == "conformance":
            # The deterministic half of the acceptance gate (#18 bullet 2). --project is
            # optional: without a task source, states read "unknown" and everything is
            # unverified (exit 1) — the checklist is still worth printing for inspection.
            from .spec_conformance import conformance_report, render_conformance

            source = load_project(args.project).task_source if args.project else None
            checklist = conformance_report(args.file, source)
            if args.json:
                _emit(checklist)
            else:
                sys.stdout.write(render_conformance(checklist))
            # Exit 1 when the batch is not demonstrably complete (any open/unknown issue).
            return 0 if checklist["complete"] else 1
        # spec file — needs the project's task source.
        if not args.project:
            p.error("--project is required for `spec file`")
        # A-priori gate (#34): with --strict, an estimate overrun refuses to file.
        if args.budget_usd is not None:
            est = estimate_budget(spec, budget_usd=args.budget_usd)
            sys.stderr.write(render_estimate(est) + "\n")
            if est["overrun"] and args.strict:
                _emit({"ok": False, "error": "a-priori estimate overruns the budget "
                       f"(${est['total_estimate_usd']:.2f} > ${args.budget_usd:.2f}); "
                       "raise --budget-usd or drop --strict to file anyway"})
                return 1
        source = load_project(args.project).task_source
        try:
            result = file_spec(spec, source, dry_run=args.dry_run)
        except SpecError as exc:
            _emit({"ok": False, "error": str(exc)})
            return 1
        # Archive the filed spec + local-id→issue-ref mapping so the conformance gate can
        # find it later (#18 bullet 2). Nothing was filed on a dry-run — nothing to record.
        if not args.dry_run:
            from .spec_intake import archive_spec

            result["archived"] = str(archive_spec(spec, result, args.archive_dir))
        _emit(result)
        return 0

    if args.cmd == "batch-plan":
        # The auto-analysis producer (#57). candidates/validate are lightweight (no run);
        # apply needs the engine. The model authors the plan — this only fetches, validates,
        # and applies. load_plan raises BatchPlanError (a clear message) on bad input.
        from .batch_plan import BatchPlanError, apply_plan, load_plan, topological_order
        from .batch_plan import validate_plan as validate_batch_plan

        def _known_ids(project_arg: str | None) -> list[str] | None:
            """Open-issue ids the plan's external edges may reference (also the candidate
            set). None when no source / no list_tasks — validate then skips external checks."""
            if not project_arg:
                return None
            source = load_project(project_arg).task_source
            lister = getattr(source, "list_tasks", None)
            if not callable(lister):
                return None
            return [t.task_id for t in lister()]

        if args.batch_cmd == "candidates":
            if not args.project:
                p.error("--project is required for `batch-plan candidates`")
            source = load_project(args.project).task_source
            lister = getattr(source, "list_tasks", None)
            if not callable(lister):
                _emit({"ok": False, "error": "task source exposes no list_tasks(label, "
                       "limit) hook — cannot fetch batch candidates"})
                return 1
            tasks = lister(label=args.label, limit=args.limit)
            candidates = [
                {"task_id": t.task_id, "title": t.title,
                 "body_excerpt": (t.body[:500] + "…") if len(t.body) > 500 else t.body,
                 "labels": t.labels, "depends_on": t.depends_on}
                for t in tasks
            ]
            _emit({"repo": getattr(source, "repo", None), "label": args.label,
                   "count": len(candidates), "candidates": candidates})
            return 0
        try:
            plan = load_plan(args.file)
        except BatchPlanError as exc:
            _emit({"ok": False, "error": str(exc)})
            return 1
        if args.batch_cmd == "validate":
            try:
                validate_batch_plan(plan, _known_ids(args.project))
            except BatchPlanError as exc:
                _emit({"ok": False, "error": str(exc)})
                return 1
            _emit({"ok": True, "tasks": len(plan["tasks"]),
                   "order": topological_order(plan)})
            return 0
        # batch-plan apply — needs the engine (root/run/project).
        if not args.root or not args.run or not args.project:
            p.error("--root, --run and --project are required for `batch-plan apply`")
        eng = _engine(args)
        try:
            result = apply_plan(eng, args.run, plan, known_ids=_known_ids(args.project),
                                dry_run=args.dry_run)
        except BatchPlanError as exc:
            _emit({"ok": False, "error": str(exc)})
            return 1
        _emit(result)
        return 0

    if args.cmd == "brainstorm":
        # The front door ABOVE intake (#2). validate/capture-print are pure (no project);
        # capture --file-selected needs a task source. The model authors the ideas — this
        # only ranks and files. load_brainstorm raises BrainstormError on any bad input.
        from .brainstorm import (
            BrainstormError,
            file_selected,
            load_brainstorm,
            rank_ideas,
            render_shortlist,
        )

        try:
            doc = load_brainstorm(args.file)
        except BrainstormError as exc:
            _emit({"ok": False, "error": str(exc)})
            return 1
        if args.brainstorm_cmd == "validate":
            _emit({"ok": True, "area": doc["area"], "ideas": len(doc["ideas"]),
                   "order": [i["title"] for i in rank_ideas(doc)]})
            return 0
        # capture: always print the durable, replayable shortlist.
        sys.stdout.write(render_shortlist(doc) + "\n")
        if args.file_selected is None:
            return 0  # no selection — just the shortlist
        try:
            selected = [int(s.strip()) for s in args.file_selected.split(",") if s.strip()]
        except ValueError:
            _emit({"ok": False, "error": "--file-selected must be comma-separated integers "
                   "(1-based shortlist ranks)"})
            return 1
        # Filing opens real issues — needs the project's task source (dry-run excepted).
        if not args.project and not args.dry_run:
            p.error("--project is required to file selected ideas (or use --dry-run)")
        source = load_project(args.project).task_source if args.project else None
        try:
            _emit(file_selected(doc, source, selected, dry_run=args.dry_run))
        except BrainstormError as exc:
            _emit({"ok": False, "error": str(exc)})
            return 1
        return 0

    if not args.root or not args.run or not args.project:
        p.error(f"--root, --run and --project are required for {args.cmd}")
    eng = _engine(args)

    # Resolve --util once: a number passes through; 'auto' probes the usage endpoint
    # (falling back to 0.0 — gates open — with the miss stated, never silent).
    util_pct = 0.0
    if hasattr(args, "util"):
        from .usage_probe import resolve_util

        util_pct, _ = resolve_util(args.util)

    if args.cmd == "init-run":
        run = eng.create_run(args.run, ExecutionLane(args.lane),
                             budget_usd=args.budget_usd, route_by_cost=args.route_by_cost,
                             route_by_capacity=args.route_by_capacity,
                             cross_provider_fallback=args.cross_provider_fallback,
                             warm_retry=args.warm_retry,
                             progress_comments=args.progress_comments)
        _emit({"created_run": run.run_id, "lane": run.lane.value,
               "budget_usd": run.budget_usd, "route_by_cost": run.route_by_cost,
               "route_by_capacity": run.route_by_capacity,
               "cross_provider_fallback": run.cross_provider_fallback,
               "warm_retry": run.warm_retry,
               "progress_comments": run.progress_comments})
    elif args.cmd == "add-task":
        from .schemas.enums import Stage

        pipeline = (
            [Stage(s.strip()) for s in args.pipeline.split(",") if s.strip()]
            if args.pipeline else None
        )
        deps = (
            [d.strip() for d in args.depends_on.split(",") if d.strip()]
            if args.depends_on else None
        )
        det = (
            [Stage(s.strip()) for s in args.deterministic_stages.split(",") if s.strip()]
            if getattr(args, "deterministic_stages", None) else None
        )
        task = eng.add_task(args.run, args.task, pipeline=pipeline,
                            depends_on=deps, provider_tag=args.provider_tag,
                            deterministic_stages=det, estimate=args.estimate,
                            model=args.model)
        _emit({"added_task": task.task_id, "title": task.title,
               "pipeline": [s.value for s in task.pipeline],
               "deterministic_stages": [s.value for s in task.deterministic_stages],
               "execution_lane": task.execution_lane.value,
               "depends_on": task.depends_on, "provider_tag": task.provider_tag,
               "model_pin": task.model_pin})
    elif args.cmd == "next":
        # Deterministic stages (intake setup, and any TEST/DELIVER a pipeline opted into
        # the ENGINE lane — #33) run in-process — drain them here so the interactive
        # supervisor only ever sees model WorkItems (never hand-creates a worktree or runs
        # `gh pr create`). Keyed on the engine-chosen lane (ExecutionMode.ENGINE), the
        # single source of truth for "deterministic", so it covers per-task opt-ins without
        # re-deriving from STAGE_SPECS. The headless scheduler dispatches these itself via
        # the registry, so this drain is the interactive lane's equivalent.
        from .schemas.enums import ExecutionMode

        # --resume applies only to the FIRST call — the one recovering a lease a crashed
        # supervisor left held (#50). Any deterministic stages drained afterward are fresh
        # dispatches with no outstanding lease, so they take the normal path.
        work = eng.next_work(args.run, args.task, util_pct=util_pct, resume=args.resume)
        while work is not None and work.lane_policy.execution_mode is ExecutionMode.ENGINE:
            result = eng.registry.resolve(work.lane_policy).dispatch(work)
            eng.record(args.run, result)
            work = eng.next_work(args.run, args.task, util_pct=util_pct)
        _emit(None if work is None else json.loads(work.model_dump_json()))
    elif args.cmd == "record":
        result = StageResult.model_validate_json(Path(args.result).read_text())
        _emit(eng.record(args.run, result))
    elif args.cmd == "dispatchable":
        from .scheduler import Scheduler

        sched = Scheduler(eng, max_concurrent=args.max_concurrent)
        ready = sched.dispatchable(args.run)
        limit = eng.capacity.dispatch_limit(util_pct, args.max_concurrent)
        _emit({"dispatchable": ready, "limit": limit, "dispatch_now": ready[:limit]})
    elif args.cmd == "run-headless":
        import time

        from adapters.execution.runners import registry_runner

        from .scheduler import Scheduler

        sched = Scheduler(eng, max_concurrent=args.max_concurrent)
        util_provider = None
        if args.util == "auto":
            from .usage_probe import read_usage

            def util_provider() -> float:  # re-probe each tick so the gate tracks reality
                usage = read_usage()
                return usage.five_hour_pct if usage else 0.0

        _emit(sched.run(
            args.run, registry_runner(eng.registry), util_pct=util_pct,
            util_provider=util_provider, sleeper=time.sleep if args.wait else None,
        ))
    elif args.cmd == "hold":
        task = eng.hold_for_approval(args.run, args.task, args.reason)
        _emit({"held": task.task_id, "state": task.state.value, "reason": args.reason})
    elif args.cmd == "approve":
        task = eng.approve(args.run, args.task, approved_by=args.by, what=args.note)
        _emit({"approved": task.task_id, "state": task.state.value, "by": args.by})
    elif args.cmd == "unpause":
        run = eng.unpause_run(args.run, raise_budget_to=args.raise_budget)
        _emit({"unpaused": run.run_id, "state": run.state.value,
               "budget_usd": run.budget_usd})
    elif args.cmd == "reject":
        task = eng.reject(args.run, args.task, rejected_by=args.by, reason=args.reason)
        _emit({"rejected": task.task_id, "state": task.state.value, "by": args.by})
    elif args.cmd == "resume":
        _emit(eng.resume(args.run))
    elif args.cmd == "status":
        _emit(eng.status(args.run))
    elif args.cmd == "watch":
        import time

        from .alerting import watch as watch_run

        final = watch_run(
            eng, args.run, interval=args.interval, stale_after_s=args.stale_after,
            sleeper=time.sleep, emit=lambda line: print(line, file=sys.stderr),
            activity=args.activity, stall_after_s=args.stall_after,
        )
        _emit({"watch": "done", "run_id": args.run, "run_state": final["run_state"],
               "progress": final["progress"]})
    elif args.cmd == "cost-report":
        _emit(eng.ledger.analysis())
    elif args.cmd == "retrospective":
        _emit(eng.retrospective(args.run))
    else:  # pragma: no cover
        p.error(f"unknown command {args.cmd}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
