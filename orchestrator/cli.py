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
    # Codex full-validation needs the project's schemas (optional hook); wire it through.
    schema_provider = getattr(project, "schema_for", None)
    registry = build_registry(include_interactive=interactive, codex_schema_provider=schema_provider)
    router = Router(execution_mode=mode, orchestrator_provider=provider)
    return Engine(store, ledger, project, router=router, registry=registry)


def _emit(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator")
    p.add_argument("--root", help="status/ledger directory for the run (not needed for validate)")
    p.add_argument("--run", help="run id (not needed for validate)")
    p.add_argument("--project", required=True,
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
    n = sub.add_parser("next")
    n.add_argument("--task", required=True)
    n.add_argument("--util", type=float, default=0.0)
    sub.add_parser("record").add_argument("--result", required=True, help="StageResult JSON file")
    d = sub.add_parser("dispatchable")
    d.add_argument("--util", type=float, default=0.0)
    d.add_argument("--max-concurrent", type=int, default=3)
    rh = sub.add_parser("run-headless", help="drive the whole run in-process (headless mode)")
    rh.add_argument("--util", type=float, default=0.0)
    rh.add_argument("--max-concurrent", type=int, default=3)
    hd = sub.add_parser("hold", help="park a task at the human approval gate")
    hd.add_argument("--task", required=True)
    hd.add_argument("--reason", required=True, help="what needs human sign-off")
    ap = sub.add_parser("approve", help="release a held task (writes the approval artifact)")
    ap.add_argument("--task", required=True)
    ap.add_argument("--by", required=True, help="who is approving")
    ap.add_argument("--note", default="", help="what is being approved")
    sub.add_parser("resume")
    sub.add_parser("status")
    sub.add_parser("cost-report", help="per-stage/-task cost breakdown + the session-reuse win")
    sub.add_parser("retrospective", help="failure retrospective (patterns + what the retries learned)")
    sub.add_parser("validate", help="check a project adapter against the engine's contract (no run needed)")

    args = p.parse_args(argv)

    if args.cmd == "validate":
        # Loading an external dir already enforces CONTRACT_VERSION + the full surface;
        # validate_config additionally reports on module-path adapters.
        config = load_project(args.project)
        missing = validate_config(config)
        _emit({"project": getattr(config, "name", None), "valid": not missing,
               "missing": missing, "contract_version": ADAPTER_CONTRACT_VERSION})
        return 1 if missing else 0

    if not args.root or not args.run:
        p.error(f"--root and --run are required for {args.cmd}")
    eng = _engine(args)

    if args.cmd == "init-run":
        run = eng.create_run(args.run, ExecutionLane(args.lane))
        _emit({"created_run": run.run_id, "lane": run.lane.value})
    elif args.cmd == "add-task":
        from .schemas.enums import Stage

        pipeline = (
            [Stage(s.strip()) for s in args.pipeline.split(",") if s.strip()]
            if args.pipeline else None
        )
        task = eng.add_task(args.run, args.task, pipeline=pipeline)
        _emit({"added_task": task.task_id, "title": task.title,
               "pipeline": [s.value for s in task.pipeline]})
    elif args.cmd == "next":
        work = eng.next_work(args.run, args.task, util_pct=args.util)
        _emit(None if work is None else json.loads(work.model_dump_json()))
    elif args.cmd == "record":
        result = StageResult.model_validate_json(Path(args.result).read_text())
        _emit(eng.record(args.run, result))
    elif args.cmd == "dispatchable":
        from .scheduler import Scheduler

        sched = Scheduler(eng, max_concurrent=args.max_concurrent)
        ready = sched.dispatchable(args.run)
        limit = eng.capacity.dispatch_limit(args.util, args.max_concurrent)
        _emit({"dispatchable": ready, "limit": limit, "dispatch_now": ready[:limit]})
    elif args.cmd == "run-headless":
        from adapters.execution.runners import registry_runner

        from .scheduler import Scheduler

        sched = Scheduler(eng, max_concurrent=args.max_concurrent)
        _emit(sched.run(args.run, registry_runner(eng.registry), util_pct=args.util))
    elif args.cmd == "hold":
        task = eng.hold_for_approval(args.run, args.task, args.reason)
        _emit({"held": task.task_id, "state": task.state.value, "reason": args.reason})
    elif args.cmd == "approve":
        task = eng.approve(args.run, args.task, approved_by=args.by, what=args.note)
        _emit({"approved": task.task_id, "state": task.state.value, "by": args.by})
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
