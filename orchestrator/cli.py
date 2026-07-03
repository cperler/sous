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


def _engine(args: argparse.Namespace) -> Engine:
    from .status_store import StatusStore

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
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
    )
    router = Router(execution_mode=mode, orchestrator_provider=provider)
    return Engine(store, ledger, project, router=router, registry=registry)


def _emit(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator")
    p.add_argument("--root", help="status/ledger directory for the run (not needed for validate)")
    p.add_argument("--run", help="run id (not needed for validate)")
    p.add_argument("--project",
                   help="project-config module (e.g. adapters.project.heysoo) or a "
                        "project-owned adapter dir (e.g. ../my-project/.orchestration)")
    p.add_argument("--mode", default="interactive", choices=["interactive", "headless"],
                   help="execution mode (config-only lane selection)")
    p.add_argument("--provider", default=None, choices=["claude", "codex"],
                   help="global provider override (ORCHESTRATOR_PROVIDER)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-run").add_argument("--lane", default="full")
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
    util_help = "5h utilization %% for the capacity gates: a number, or 'auto' to probe"
    n = sub.add_parser("next")
    n.add_argument("--task", required=True)
    n.add_argument("--util", default="0", help=util_help)
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
    hd = sub.add_parser("hold", help="park a task at the human approval gate")
    hd.add_argument("--task", required=True)
    hd.add_argument("--reason", required=True, help="what needs human sign-off")
    ap = sub.add_parser("approve", help="release a held task (writes the approval artifact)")
    ap.add_argument("--task", required=True)
    ap.add_argument("--by", required=True, help="who is approving")
    ap.add_argument("--note", default="", help="what is being approved")
    sub.add_parser("unpause", help="release a PAUSED run (e.g. after the batch circuit "
                                   "breaker tripped and the systemic cause is fixed)")
    sub.add_parser("resume")
    sub.add_parser("status")
    sub.add_parser("cost-report", help="per-stage/-task cost breakdown + the session-reuse win")
    sub.add_parser("retrospective", help="failure retrospective (patterns + what the retries learned)")
    sub.add_parser("validate", help="check a project adapter against the engine's contract (no run needed)")
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

    args = p.parse_args(argv)

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

    if args.cmd == "util":
        # The capacity sensor (needs no run/project): probe the usage endpoint and emit
        # the numbers the --util gates consume. A probe miss is an explicit field, not
        # an error — callers fall back to 0.0 (gates open) knowingly.
        from dataclasses import asdict

        from .usage_probe import read_usage

        usage = read_usage()
        _emit({"available": usage is not None, **(asdict(usage) if usage else {})})
        return 0

    if args.cmd == "validate":
        # Loading an external dir already enforces CONTRACT_VERSION + the full surface;
        # validate_config additionally reports on module-path adapters.
        config = load_project(args.project)
        missing = validate_config(config)
        _emit({"project": getattr(config, "name", None), "valid": not missing,
               "missing": missing, "contract_version": ADAPTER_CONTRACT_VERSION})
        return 1 if missing else 0

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
        run = eng.create_run(args.run, ExecutionLane(args.lane))
        _emit({"created_run": run.run_id, "lane": run.lane.value})
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
        task = eng.add_task(args.run, args.task, pipeline=pipeline,
                            depends_on=deps, provider_tag=args.provider_tag)
        _emit({"added_task": task.task_id, "title": task.title,
               "pipeline": [s.value for s in task.pipeline],
               "depends_on": task.depends_on, "provider_tag": task.provider_tag})
    elif args.cmd == "next":
        # Deterministic stages (e.g. intake setup) run in-process on the ENGINE lane —
        # drain them here so the interactive supervisor only ever sees model WorkItems
        # (never hand-creates a worktree). The headless scheduler dispatches them itself
        # via the registry, so this drain is the interactive lane's equivalent.
        from .stages import STAGE_SPECS

        work = eng.next_work(args.run, args.task, util_pct=util_pct)
        while work is not None and STAGE_SPECS[work.stage].deterministic:
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
        run = eng.unpause_run(args.run)
        _emit({"unpaused": run.run_id, "state": run.state.value})
    elif args.cmd == "resume":
        _emit(eng.resume(args.run))
    elif args.cmd == "status":
        _emit(eng.status(args.run))
    elif args.cmd == "cost-report":
        _emit(eng.ledger.analysis())
    elif args.cmd == "retrospective":
        _emit(eng.retrospective(args.run))
    else:  # pragma: no cover
        p.error(f"unknown command {args.cmd}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
