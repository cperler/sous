"""Engine CLI — the supervisor's Bash entry points (target.md §3).

The supervisor loop is: ``ready`` -> ``next`` (get a WorkItem) -> [the execution
lane runs it] -> ``record`` (ingest the StageResult) -> repeat. The CLI speaks JSON
on stdout so a Bash/skill supervisor can drive it. It never calls a model.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from adapters.execution.runners import build_registry
from adapters.project.base import ProjectConfig

from .cost_ledger import CostLedger
from .engine import Engine
from .routing import Router
from .schemas.enums import ExecutionLane, ExecutionMode, Provider
from .schemas.work import StageResult


def _load_project(module_path: str) -> ProjectConfig:
    mod = importlib.import_module(module_path)
    factory = getattr(mod, "get_config", None)
    if factory is None:
        raise SystemExit(f"project module {module_path!r} has no get_config()")
    return factory()


def _engine(args: argparse.Namespace) -> Engine:
    from .status_store import StatusStore

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    store = StatusStore(root)
    ledger = CostLedger(root / "stage-costs.jsonl")
    project = _load_project(args.project)

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
    p.add_argument("--root", required=True, help="status/ledger directory for the run")
    p.add_argument("--run", required=True, help="run id")
    p.add_argument("--project", required=True,
                   help="project-config module (e.g. adapters.project.heysoo)")
    p.add_argument("--mode", default="interactive", choices=["interactive", "headless"],
                   help="execution mode (config-only lane selection)")
    p.add_argument("--provider", default=None, choices=["claude", "codex"],
                   help="global provider override (ORCHESTRATOR_PROVIDER)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-run").add_argument("--lane", default="full")
    sub.add_parser("add-task").add_argument("--task", required=True)
    n = sub.add_parser("next")
    n.add_argument("--task", required=True)
    n.add_argument("--util", type=float, default=0.0)
    sub.add_parser("record").add_argument("--result", required=True, help="StageResult JSON file")
    r = sub.add_parser("ready")
    r.add_argument("--util", type=float, default=0.0)
    d = sub.add_parser("dispatchable")
    d.add_argument("--util", type=float, default=0.0)
    d.add_argument("--max-concurrent", type=int, default=3)
    rh = sub.add_parser("run-headless", help="drive the whole run in-process (headless mode)")
    rh.add_argument("--util", type=float, default=0.0)
    rh.add_argument("--max-concurrent", type=int, default=3)
    sub.add_parser("resume")
    sub.add_parser("status")

    args = p.parse_args(argv)
    eng = _engine(args)

    if args.cmd == "init-run":
        run = eng.create_run(args.run, ExecutionLane(args.lane))
        _emit({"created_run": run.run_id, "lane": run.lane.value})
    elif args.cmd == "add-task":
        task = eng.add_task(args.run, args.task)
        _emit({"added_task": task.task_id, "title": task.title})
    elif args.cmd == "next":
        work = eng.next_work(args.run, args.task, util_pct=args.util)
        _emit(None if work is None else json.loads(work.model_dump_json()))
    elif args.cmd == "record":
        result = StageResult.model_validate_json(Path(args.result).read_text())
        _emit(eng.record(args.run, result))
    elif args.cmd == "ready":
        _emit({"ready": eng.ready(args.run, util_pct=args.util)})
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
    elif args.cmd == "resume":
        _emit(eng.resume(args.run))
    elif args.cmd == "status":
        _emit(eng.status(args.run))
    else:  # pragma: no cover
        p.error(f"unknown command {args.cmd}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
