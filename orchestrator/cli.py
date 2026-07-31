"""Engine CLI — supervisor and unattended entrypoints (target.md §3).

Three operational modes share this CLI:

* **Interactive supervisor loop** — the human-in-the-loop flow where a
  Bash/skill supervisor drives ``next`` → [lane executes] → ``record`` → repeat.
  The CLI speaks JSON on stdout; the supervisor owns the outer loop.

* **Headless in-process** (``run-headless``) — the engine scheduler drives the
  whole run autonomously, dispatching in-process over the registry runners without
  a human supervisor. Suitable for scripted or CI contexts.

* **Unattended queue drain** (``enqueue`` + ``run-queue``) — a cron-friendly
  front door for the headless lane. ``enqueue`` atomically appends a batch entry
  to a ``ralph-queue.json``-style queue file (no engine needed). ``run-queue``
  drains the queue batch-by-batch under the claim-in-place protocol (#279): each
  entry becomes a stable run id (derived from ``enqueued_at``) recorded as an
  in-place claim on the entry, created-or-reused, and driven to terminal through
  ``Scheduler.run`` — only then is the entry dequeued, so no process death can
  lose a batch. Each derived run gets its OWN nested store under ``--root``
  (#281); the queue file and learnings KB stay at the shared root. See
  ``orchestrator.queue_file`` for the contract.

The CLI never calls a model. All model-level work is delegated to the execution
lane runners wired into the engine registry.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Callable

from .cost_ledger import CostLedger
from .driver_log import DEFAULT_HEARTBEAT_INTERVAL_S
from .engine import DEFAULT_ABANDON_MIN_IDLE_S, Engine
from .lane_loader import build_registry
from .ports.project import ADAPTER_CONTRACT_VERSION
from .project_loader import load_project, validate_config
from .routing import Router
from .schemas.enums import ExecutionLane, ExecutionMode, Provider
from .schemas.work import StageResult


def _budget_usd(raw: str) -> float:
    """argparse type for a USD budget flag: a finite amount > 0 (#274).

    Mirrors the Engine-side contract (``_validated_budget``) at the parse boundary so a
    ``--budget-usd 0`` is a clean usage error instead of an engine traceback (a zero cap
    used to crash the first dispatch with ZeroDivisionError)."""
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a number") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a finite USD amount > 0, got {raw}")
    return value


def _auto_util_provider() -> Callable[[], float]:
    """A live 5-hour-usage util probe: re-probes each tick so the gate tracks reality
    (0.0 when no usage snapshot is available). Shared by the queue-drive and headless
    schedulers so neither nests a per-branch ``def`` in ``main``'s scope."""
    from .usage_probe import read_usage

    def provider() -> float:
        usage = read_usage()
        return usage.five_hour_pct if usage else 0.0

    return provider


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


def _resolve_store_root(root: Path, run: str | None, *, force_nest: bool = False) -> Path:
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
    directly, preserving callers that already point at the per-run dir.

    ``force_nest`` (the ``--shared-root`` flag, #91) closes the day-one bootstrapping
    gap: the structural heuristic cannot recognize a *fresh* shared ``runs/`` dir (no
    KB, no sibling run stores yet), so the very first run under it would land flat.
    When the caller asserts ``--root`` is the shared runs-root, force the nest even
    with no markers. The established-flat guard still wins so an in-progress flat run
    stays stable."""
    if not run:
        return root
    if (root / f"status-{run}.json").exists():
        return root  # this run is already established flat here — stay put
    nested = root / run
    if nested.is_dir() or force_nest or _is_shared_runs_root(root, run):
        return nested
    return root


# Commands whose store dir is resolved through _engine() — the only place
# --shared-root (force_nest) has any effect (#101). Every other subcommand ignores
# the flag, so passing it there is a no-op worth warning about (mis-positioned flag).
_ENGINE_COMMANDS = frozenset({
    "init-run", "add-task", "next", "record", "dispatchable", "run-headless",
    "hold", "approve", "unpause", "reject", "abandon", "retire", "resume", "status", "refresh-spec",
    "watch", "cost-report", "retrospective", "trunk-gate",
})


def _consumes_shared_root(args: argparse.Namespace) -> bool:
    """True when the parsed command actually routes through ``_engine()`` and so
    honors ``--shared-root``. Only the per-run engine commands and ``batch-plan
    apply`` build an Engine; kb/dashboard/gc/tail/util/statusline/validate/spec/
    brainstorm/batch-plan-candidates|validate all ignore the flag (#101)."""
    if args.cmd in _ENGINE_COMMANDS:
        return True
    return args.cmd == "batch-plan" and getattr(args, "batch_cmd", None) == "apply"


def _engine(args: argparse.Namespace) -> Engine:
    """Build and return a fully-wired ``Engine`` from the parsed CLI args.

    Resolves the per-run store directory (auto-nesting under a shared runs-root
    when appropriate), then constructs the ``StatusStore``, ``CostLedger``,
    project adapter, ``Router``, and execution-lane ``Registry`` in one shot.

    Execution mode is derived from ``--mode`` but overridden to
    ``HEADLESS`` for ``run-headless`` — that command drives the engine in-process
    and must never open an interactive session. (``run-queue`` no longer routes
    through here: it builds one engine PER derived run via its own factory, #281.)
    """
    from .status_store import StatusStore

    root = _resolve_store_root(
        Path(args.root), getattr(args, "run", None),
        force_nest=getattr(args, "shared_root", False),
    )
    root.mkdir(parents=True, exist_ok=True)
    if root != Path(args.root):
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
        mode = ExecutionMode.HEADLESS  # drives in-process; force the lane
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


def _emit(obj: object) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` (or ``sys.argv[1:]``) and dispatch to the matching subcommand.

    Returns the process exit code (0 = success, 1 = any handled error).

    Subcommands fall into three groups:

    * **Engine commands** (``init-run``, ``add-task``, ``next``, ``record``, ``run-headless``,
      ``batch-plan``, …) — all route through ``_engine()`` which resolves ``--root``/``--run``
      to a per-run store directory, builds an ``Engine``, and delegates.
    * **Read-only board** (``dashboard``) — reads every store under ``--root`` via
      ``dashboard_snapshot`` and renders to the terminal.  Two non-default modes are available:
      ``--watch`` (clear-screen polling loop) and ``--serve`` (HTTP server mode added by #94).
      ``--serve`` binds ``web_dashboard.serve`` on ``--host``/``--port`` and forwards
      ``--limit``/``--all``/``--stale-after`` as ``snap_kwargs``; the call blocks until Ctrl-C.
    * **Cross-run learnings KB** (``kb show``/``kb add``) — reads/appends
      ``<runs-root>/learnings-kb.jsonl``.
    """
    p = argparse.ArgumentParser(prog="orchestrator")
    p.add_argument("--root",
                   help="runs-root or per-run store dir (not needed for validate). Per-run "
                        "commands auto-nest under <root>/<run>/ when <root> is a shared "
                        "runs-root (holds a learnings-kb.jsonl or other runs' stores), so "
                        "--root runs is the natural spelling shared with dashboard/kb/tail")
    p.add_argument("--shared-root", action="store_true",
                   help="assert --root IS the shared runs-root and force per-run nesting to "
                        "<root>/<run>/ even with no markers (#91). Closes the day-one gap the "
                        "auto-detect heuristic misses: a FRESH runs/ dir holds no KB or sibling "
                        "stores yet, so the first run would otherwise land flat. Pass this when "
                        "--root is the top-level runs/ dir")
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
    ir.add_argument("--budget-usd", type=_budget_usd, default=None,
                    help="per-run metered-spend budget in USD (#34): a soft warning at "
                         "80%%, a hard PAUSE at/after the budget. Must be > 0")
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
    ir.add_argument("--max-filed-followups", type=int, default=None,
                    help="run-wide default cap on how many non-blocking review findings each "
                         "task FILES as follow-up issues (#196): set once here so every task in "
                         "the run shares a non-default baseline without repeating "
                         "--max-filed-followups on every add-task. A per-task override still "
                         "wins; omitted, the engine default applies. Must be >= 0")
    ir.add_argument("--review-workflow", action="store_true",
                    help="run REVIEW as a multi-agent find→verify panel (#73): independent "
                         "finder lenses (code/spec/tests, plus design on a frontend change) "
                         "whose findings are adversarially verified, instead of one "
                         "mega-prompt reviewer. Only on lanes that can execute a plan (not "
                         "codex); micro/lite presets, a loaded API, and a thinning budget all "
                         "fall back to the single reviewer. Default off")
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
    at.add_argument("--effort", default=None, choices=["low", "medium", "high"],
                    help="per-task reasoning-effort pin (#96): overrides the stage-spec "
                         "defaults (scope/implement high, test/review medium, deliver low) "
                         "on model-lane stages; translated per lane (claude --effort, codex "
                         "model_reasoning_effort). Honored by the capacity downshift like "
                         "--model pins")
    at.add_argument("--max-filed-followups", type=int, default=None,
                    help="per-task cap on how many non-blocking review findings are FILED as "
                         "follow-up issues (#191); overrides the engine-wide default for a task "
                         "type with a different expected review surface (a micro fix vs a full "
                         "feature). Omitted, the engine default applies; must be >= 0")
    util_help = "5h utilization %% for the capacity gates: a number, or 'auto' to probe"
    n = sub.add_parser("next")
    n.add_argument("--task", required=True)
    n.add_argument("--util", default="0", help=util_help)
    n.add_argument("--resume", action="store_true",
                   help="re-emit the pending WorkItem for a task whose supervisor crashed "
                        "holding the dispatch lease (bypasses the lease/cooldown guard so a "
                        "crashed supervisor recovers its item without hand-editing state; #50)")
    sub.add_parser("record").add_argument("--result", required=True, help="StageResult JSON file")
    d = sub.add_parser(
        "dispatchable",
        help="list DAG-ready tasks and in-flight capacity state (#97): "
             "'dispatchable' = unleased tasks whose deps are met; "
             "'in_flight'/'in_flight_count' = tasks with a live dispatch lease right now. "
             "Remaining headroom = limit - in_flight_count, and 'dispatch_now' is "
             "'dispatchable' already sliced to that headroom (in-flight leases subtracted, #135). "
             "Re-check before every follow-on dispatch so the cap binds across concurrent "
             "background invocations.",
    )
    d.add_argument("--util", default="0", help=util_help)
    d.add_argument("--max-concurrent", type=int, default=3)
    rh = sub.add_parser(
        "run-headless",
        help="drive the whole run in-process (headless mode). FOREGROUND: this process "
             "owns the run for its entire duration and the provider processes are its "
             "children — Ctrl-C kills them too. Monitor from a SEPARATE terminal "
             "(`orchestrator watch`). Re-invoking after a kill resumes: leases left by "
             "the dead driver are reclaimed at the same attempt (#313). Exits non-zero if "
             "it stops with leases it may not reclaim (another live driver).",
    )
    rh.add_argument("--util", default="0", help=util_help)
    rh.add_argument("--max-concurrent", type=int, default=3)
    rh.add_argument("--wait", action="store_true",
                    help="sleep through capacity stalls / rate-limit cooldowns instead of "
                         "returning (the old capacity_wait_loop)")
    rh.add_argument("--heartbeat-interval", type=int, default=DEFAULT_HEARTBEAT_INTERVAL_S,
                    help="seconds between driver heartbeats while WAITING out a capacity "
                         "stall or cooldown (#323); each is a line in runs/<run>/driver.jsonl "
                         f"and on stderr (default {DEFAULT_HEARTBEAT_INTERVAL_S})")
    eq = sub.add_parser("enqueue", help="append one batch entry to a queue file for the "
                                        "unattended run-queue loop (#1); no engine needed")
    eq.add_argument("--queue-file", required=True, help="ralph-queue.json-style queue file")
    eq.add_argument("--tasks", required=True,
                    help="comma-separated task ids for this batch")
    eq.add_argument("--branch", default=None, help="optional batch branch (recorded metadata)")
    rq = sub.add_parser("run-queue", help="unattended (cron) entrypoint (#1): drain a queue "
                                          "file batch-by-batch, driving each run in-process")
    rq.add_argument("--queue-file", required=True, help="ralph-queue.json-style queue file")
    rq.add_argument("--util", default="0", help=util_help)
    rq.add_argument("--max-concurrent", type=int, default=3)
    rq.add_argument("--lane", default="full", help="lane preset for created runs (default full)")
    rq.add_argument("--idle-timeout", type=int, default=300,
                    help="with --wait: seconds to keep polling an empty queue before exiting")
    rq.add_argument("--poll-interval", type=int, default=15,
                    help="with --wait: empty-queue poll interval seconds (default 15)")
    rq.add_argument("--wait", action="store_true",
                    help="idle-wait on an empty queue and sleep through capacity/cooldown "
                         "stalls (the cron/daemon mode); default is a single drain pass")
    rq.add_argument("--owner", default="default",
                    help="stable consumer identity for the claim protocol (#279): a "
                         "restarted consumer with the same owner resumes its own stale "
                         "claims. Each concurrent consumer must use a distinct owner; "
                         "sharing one would otherwise double-drive a run (guarded on "
                         "this host by a process-lifetime lock)")
    rq.add_argument("--release-claim", action="store_true",
                    help="admin recovery: release the head claim without draining the "
                         "queue; refuses a provably live local owner")
    rq.add_argument("--force", action="store_true",
                    help="with --release-claim, override the live-owner refusal")
    sub.add_parser("util", help="probe the account's 5h/7d utilization (feeds --util)")
    sl = sub.add_parser("statusline",
                        help="one-line 5h/7d utilization for the Claude Code status bar "
                             "(reads the same usage cache as util; quiet on a probe miss)")
    sl.add_argument("--watch", action="store_true",
                    help="clear-screen + reprint on a loop (supervise a batch from a terminal)")
    sl.add_argument("--interval", type=float, default=30,
                    help="--watch refresh interval seconds (amortizes over the 2-min cache)")
    hd = sub.add_parser("hold", help="park a task at the human approval gate")
    hd.add_argument("--task", required=True)
    hd.add_argument("--reason", required=True, help="what needs human sign-off")
    ap = sub.add_parser("approve", help="release a held task (writes the approval artifact)")
    ap.add_argument("--task", required=True)
    ap.add_argument("--by", required=True, help="who is approving")
    ap.add_argument("--note", default="", help="what is being approved")
    up = sub.add_parser("unpause", help="release a PAUSED run (e.g. after the batch circuit "
                                        "breaker tripped and the systemic cause is fixed)")
    up.add_argument("--raise-budget", type=_budget_usd, default=None,
                    help="on a budget-exhausted pause (#34): resume with this NEW budget "
                         "ceiling (re-arms the soft warning), must be > 0. Omit to drop "
                         "the cap entirely")
    rj = sub.add_parser("reject", help="confirm-and-close a held infeasible task (writes the rejection artifact)")
    rj.add_argument("--task", required=True)
    rj.add_argument("--by", required=True, help="who is rejecting")
    rj.add_argument("--reason", required=True, help="why the task is infeasible")
    ab = sub.add_parser("abandon", help="finalize a task whose run was killed mid-dispatch "
                                        "(#82): release the outstanding lease and drive the "
                                        "task terminal without a hand-crafted synthetic result")
    ab.add_argument("--task", required=True)
    ab.add_argument("--reason", required=True, help="why the dispatch is being abandoned")
    ab.add_argument("--disposition", choices=["failed", "rejected"], default="failed",
                    help="terminal state: 'failed' (execution died) or 'rejected' "
                         "(close-infeasible, writes the rejection artifact). Default failed")
    ab.add_argument("--min-idle-s", type=int, default=DEFAULT_ABANDON_MIN_IDLE_S,
                    help="refuse if the dispatch's provider stream grew within this many "
                         "seconds (it may still be alive); default "
                         f"{DEFAULT_ABANDON_MIN_IDLE_S}")
    ab.add_argument("--force", action="store_true",
                    help="override the liveness guard (the process is known dead)")
    rt = sub.add_parser("retire", help="retire a run the human superseded (#257): drive every "
                                       "non-terminal task to SUPERSEDED and finalize the run, "
                                       "with NO note published to any issue")
    rt.add_argument("--reason", required=True, help="why the run is being retired")
    rt.add_argument("--by", required=True, help="who is retiring the run")
    rt.add_argument("--superseded-by", default=None,
                    help="run id of the successor that replaces this run, when there is one")
    rt.add_argument("--force", action="store_true",
                    help="retire even though a task still holds an outstanding dispatch "
                         "(the process is known dead)")
    tg = sub.add_parser("trunk-gate",
                        help="post-merge integrity gate (#229): run the project adapter's "
                             "verification commands over a merged-trunk checkout and auto-file "
                             "a remediation task when trunk is red (non-zero exit on red)")
    tg.add_argument("-C", "--cwd", default=".",
                    help="the merged-trunk checkout to gate (default: repo root)")
    tg.add_argument("--no-file-fix", dest="file_fix", action="store_false",
                    help="report only — do NOT auto-file a remediation task on red")
    tg.set_defaults(file_fix=True)
    sub.add_parser("resume")
    st = sub.add_parser("status")
    st.add_argument("--check-spec", action="store_true",
                    help="also re-read each non-terminal task's spec from the task source "
                         "and flag one whose upstream issue has diverged from the snapshot "
                         "its prompts render from (#271; costs one source round-trip per "
                         "task, so the default poll stays offline)")
    rs = sub.add_parser("refresh-spec",
                        help="re-read a task's title/body from the task source onto its Task "
                             "doc (#271): the sanctioned way to land an amended issue on an "
                             "in-flight run, instead of rebuilding it or hand-patching JSON")
    rs.add_argument("--task", required=True)
    rs.add_argument("--check", action="store_true",
                    help="dry run: report what a refresh would change, write nothing")
    rs.add_argument("--force", action="store_true",
                    help="refresh even while the task holds a dispatch lease — that stage's "
                         "prompt was already rendered from the old snapshot and keeps it")
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
    cr = sub.add_parser("cost-report", help="per-stage/-task cost breakdown + the session-reuse win")
    cr.add_argument("--by-effort", action="store_true",
                    help="instead of the default report, split spend/duration/retry+failure "
                         "rates by (stage, effort, model) to tune the #96 per-stage effort table (#141)")
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
    # --watch and --serve are two different output modes; passing both used to let
    # --serve silently win (#121). Make them mutually exclusive so argparse errors out.
    db_mode = db.add_mutually_exclusive_group()
    db_mode.add_argument("--watch", action="store_true", help="clear-screen + reprint on a loop")
    db_mode.add_argument("--serve", action="store_true",
                         help="serve a read-only web dashboard (polls for run updates) instead of printing")
    db.add_argument("--port", type=int, default=8787, help="--serve bind port")
    db.add_argument("--host", default="127.0.0.1", help="--serve bind host (default localhost only)")
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

    # --shared-root lives on the global parser (so it can precede any engine command),
    # but it only affects per-run store nesting inside _engine(). A user who mistypes
    # its position onto a non-engine command would otherwise have it silently dropped;
    # warn instead of erroring so a legitimate global-position pass to an engine command
    # stays valid (#101).
    if getattr(args, "shared_root", False) and not _consumes_shared_root(args):
        print(
            f"warning: --shared-root is ignored by the '{args.cmd}' command; it only "
            "affects per-run store nesting for engine commands (init-run/add-task/next/"
            "record/dispatchable/run-headless/... and batch-plan apply)",
            file=sys.stderr,
        )

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
        from .engine import _ref_safe
        from .gitcmd import run_git as _git

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
        stream_path = find_current_stream(Path(args.root), args.task, args.stage)
        if stream_path is None:
            print(f"(no live stream for task {args.task} — interactive/ENGINE lane, "
                  "or nothing dispatched yet)")
            return 0
        if args.follow:
            import contextlib
            import time

            with contextlib.suppress(KeyboardInterrupt):  # interactive Ctrl-C ends the follow
                follow_stream(stream_path, emit=print, sleeper=time.sleep,
                              lines=args.lines, poll_interval=args.interval)
            return 0
        for line in read_tail(stream_path, lines=args.lines) or []:
            print(line)
        return 0

    if args.cmd == "dashboard":
        # Cross-session board (#6): reads every runs/<id>/ store under --root. Needs --project
        # to build the per-run read-only engine (like `status`), but NOT --run (it spans runs).
        from .dashboard import (
            DashboardSnapshotKwargs,
            dashboard_snapshot,
            default_engine_factory,
            render_dashboard,
            render_watch,
        )
        from .usage_probe import read_usage

        if not args.root or not args.project:
            p.error("--root and --project are required for dashboard")
        factory = default_engine_factory(args.project, mode=args.mode, provider=args.provider)
        snap_kw: DashboardSnapshotKwargs = {
            "stale_after_s": args.stale_after,
            "limit": args.limit,
            "show_all": args.all,
            "engine_factory": factory,
            "usage_reader": read_usage,
        }
        if args.serve:
            # Read-only web skin (#94): serve dashboard_snapshot()/stream_probe as JSON + a
            # self-contained polling page. Same runs-root + read-only engine as the text board.
            from .web_dashboard import serve as serve_web

            serve_web(
                args.root, factory, host=args.host, port=args.port, usage_reader=read_usage,
                snap_kwargs=dict(
                    stale_after_s=args.stale_after, limit=args.limit, show_all=args.all,
                ),
                on_ready=lambda url: print(f"orchestrator dashboard serving at {url} (Ctrl-C to stop)"),
            )
            return 0
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
        from .usage_probe import format_statusline, read_usage, watch_statusline

        if args.watch:
            import time

            # Ctrl-C ends the loop cleanly — watch_statusline swallows KeyboardInterrupt itself.
            watch_statusline(emit=print, sleeper=time.sleep, interval=args.interval)
            return 0
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
        from .batch_plan import BatchPlanError, apply_plan, load_plan
        from .batch_plan import topological_order as batch_topological_order
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
                   "order": batch_topological_order(plan)})
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

    if args.cmd == "enqueue":
        # Unattended-queue producer (#1): append one batch entry to the queue file and
        # exit. No engine/project/run — enqueue is pure file I/O so a cron job (or a human)
        # can top up the queue without touching a store. Lock-free atomic append per §1.8.
        from .queue_file import QueueError, QueueFile, make_entry

        tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
        try:
            entry = QueueFile(args.queue_file).append(make_entry(tasks, args.branch))
        except QueueError as exc:
            _emit({"ok": False, "error": str(exc)})
            return 1
        _emit({"ok": True, "queue_file": args.queue_file, "enqueued": entry})
        return 0

    if args.cmd == "run-queue":
        # Unattended (cron) entrypoint (#1): drain the queue file batch-by-batch, driving
        # each derived run in-process (headless). Needs --root/--project but NOT --run —
        # run ids are derived from each batch's enqueued_at, so one launch can process
        # many runs. Deliberately NOT via _engine(args): a single pre-built engine would
        # share one StatusStore/CostLedger across every derived run (#281), so instead a
        # factory builds a FRESH engine per claimed entry, rooted at <root>/<run_id>/ —
        # the same runs-root nesting _resolve_store_root gives per-run commands. The
        # queue file and the learnings KB (<root>/learnings-kb.jsonl) stay at the shared
        # --root, so enqueue producers and kb/dashboard discovery are unchanged.
        import time

        from .lane_loader import registry_runner
        from .queue_file import QueueError, QueueFile, drive_queue
        from .scheduler import AnyRunner
        from .status_store import StatusStore

        queue = QueueFile(args.queue_file)
        if args.force and not args.release_claim:
            p.error("--force requires --release-claim")
        if args.release_claim:
            try:
                released = queue.release_head_claim(force=args.force)
            except QueueError as exc:
                _emit({"ok": False, "error": str(exc)})
                return 1
            _emit({"ok": True, "released": released})
            return 0

        if not args.root or not args.project:
            p.error("--root and --project are required for run-queue")
        from .usage_probe import resolve_util

        util_pct, _ = resolve_util(args.util)
        util_provider = _auto_util_provider() if args.util == "auto" else None

        def _queue_engine(run_id: str) -> tuple[Engine, AnyRunner]:
            """The ``EngineFactory`` for this drain (#281): a FRESH engine + runner
            rooted at ``<--root>/<run_id>/``, so the derived run's StatusStore,
            CostLedger, and stage logs never mix with another run's. Called by
            ``drive_queue`` once per claimed entry, only after the claim has fixed
            ``run_id``."""
            root = Path(args.root) / run_id
            root.mkdir(parents=True, exist_ok=True)
            project = load_project(args.project)
            provider = Provider(args.provider) if getattr(args, "provider", None) else None
            schema_provider = getattr(project, "schema_for", None)
            registry = build_registry(
                include_interactive=False,  # run-queue drives in-process (forced HEADLESS)
                headless_schema_provider=schema_provider,
                codex_schema_provider=schema_provider,
                setup_project=project,
                run_log_root=root,  # #56/#281: stages/<task>/ logs nest per run
            )
            router = Router(
                execution_mode=ExecutionMode.HEADLESS, orchestrator_provider=provider
            )
            eng = Engine(
                StatusStore(root), CostLedger(root / "stage-costs.jsonl"), project,
                router=router, registry=registry,
            )
            return eng, registry_runner(registry)

        try:
            summary = drive_queue(
                queue, _queue_engine, owner=args.owner,
                lane=ExecutionLane(args.lane), util_pct=util_pct,
                util_provider=util_provider,
                sleeper=time.sleep if args.wait else None,
                max_concurrent=args.max_concurrent,
                idle_timeout_s=args.idle_timeout, poll_interval_s=args.poll_interval,
            )
        except QueueError as exc:
            _emit({"ok": False, "error": str(exc)})
            return 1
        _emit(summary)
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
        from .errors import RunExistsError

        try:
            run = eng.create_run(args.run, ExecutionLane(args.lane),
                                 budget_usd=args.budget_usd,
                                 route_by_cost=args.route_by_cost,
                                 route_by_capacity=args.route_by_capacity,
                                 cross_provider_fallback=args.cross_provider_fallback,
                                 warm_retry=args.warm_retry,
                                 progress_comments=args.progress_comments,
                                 max_filed_followups=args.max_filed_followups,
                                 review_workflow=args.review_workflow)
        except RunExistsError as exc:
            # Refuse loudly and write NOTHING (#280): re-initializing an existing run id
            # would orphan its task docs and erase its refs/DAG/state/settings.
            _emit({"ok": False, "error": f"{exc}. Pick a new --run id "
                                         "(there is no overwrite of an existing run)."})
            return 1
        _emit({"created_run": run.run_id, "lane": run.lane.value,
               "budget_usd": run.budget_usd, "route_by_cost": run.route_by_cost,
               "route_by_capacity": run.route_by_capacity,
               "cross_provider_fallback": run.cross_provider_fallback,
               "warm_retry": run.warm_retry,
               "progress_comments": run.progress_comments,
               "max_filed_followups": run.max_filed_followups,
               "review_workflow": run.review_workflow})
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
                            model=args.model, effort=args.effort,
                            max_filed_followups=args.max_filed_followups)
        _emit({"added_task": task.task_id, "title": task.title,
               "pipeline": [s.value for s in task.pipeline],
               "deterministic_stages": [s.value for s in task.deterministic_stages],
               "execution_lane": task.execution_lane.value,
               "depends_on": task.depends_on, "provider_tag": task.provider_tag,
               "model_pin": task.model_pin, "effort_pin": task.effort_pin})
    elif args.cmd == "next":
        # Deterministic stages (intake setup, and any TEST/DELIVER a pipeline opted into
        # the ENGINE lane — #33) run in-process — drain them here so the interactive
        # supervisor only ever sees model WorkItems (never hand-creates a worktree or runs
        # `gh pr create`). Keyed on the engine-chosen lane (ExecutionMode.ENGINE), the
        # single source of truth for "deterministic", so it covers per-task opt-ins without
        # re-deriving from STAGE_SPECS. The headless scheduler dispatches these itself via
        # the registry, so this drain is the interactive lane's equivalent.
        # --resume applies only to the FIRST call — the one recovering a lease a crashed
        # supervisor left held (#50). Any deterministic stages drained afterward are fresh
        # dispatches with no outstanding lease, so they take the normal path.
        work = eng.next_work(args.run, args.task, util_pct=util_pct, resume=args.resume)
        while work is not None and work.lane_policy.execution_mode is ExecutionMode.ENGINE:
            stage_result = eng.registry.resolve(work.lane_policy).dispatch(work)
            eng.record(args.run, stage_result)
            work = eng.next_work(args.run, args.task, util_pct=util_pct)
        _emit(None if work is None else json.loads(work.model_dump_json()))
    elif args.cmd == "record":
        from .errors import ContractError

        stage_result = StageResult.model_validate_json(Path(args.result).read_text())
        try:
            _emit(eng.record(args.run, stage_result))
        except ContractError as exc:
            # #311: a result that does not answer the outstanding dispatch (garbled/copied
            # content_hash, wrong work item, replay) is refused. Report it as the CLI's
            # machine-readable error shape rather than a traceback: the supervisor reads
            # this command's JSON, and a traceback on stdout-less stderr is how a refusal
            # gets mistaken for a transport hiccup. The engine already evented it.
            _emit({"ok": False, "recorded": False, "error": str(exc),
                   "task_id": stage_result.task_id, "stage": stage_result.stage.value,
                   "work_item_id": stage_result.work_item_id})
            return 1
    elif args.cmd == "dispatchable":
        from .scheduler import Scheduler

        sched = Scheduler(eng, max_concurrent=args.max_concurrent)
        ready = sched.dispatchable(args.run)
        limit = eng.capacity.dispatch_limit(util_pct, args.max_concurrent)
        # in_flight = tasks with a live dispatch lease. The per-task supervisor
        # (orchestrate-batch-interactive) sizes remaining headroom as `limit - in_flight`
        # so concurrency binds across concurrently-live background invocations, not just
        # within one round — re-checked before every follow-on dispatch (#97).
        in_flight = eng.in_flight(args.run)
        # Invariant (#135): dispatch_now = DAG-ready ∩ remaining headroom AFTER
        # in-flight leases — i.e. `limit - in_flight_count`, clamped at 0. Slicing
        # by the raw `limit` (pre-#97) over-counted free slots whenever tasks were
        # already leased, so the pre-sliced set is bounded by the real headroom here.
        headroom = max(0, limit - len(in_flight))
        _emit({"dispatchable": ready, "limit": limit, "dispatch_now": ready[:headroom],
               "in_flight": in_flight, "in_flight_count": len(in_flight)})
    elif args.cmd == "run-headless":
        import time

        from .lane_loader import registry_runner
        from .scheduler import EXIT_BLOCKED_ORPHANED, Scheduler

        # #323: the driver's own telemetry goes to `runs/<run>/driver.jsonl` (durable,
        # retained) AND is mirrored to stderr as it is written, so the terminal that
        # launched a 45-minute driver is no longer silent for its whole life — while stdout
        # stays exactly one JSON status document for scripted callers.
        sched = Scheduler(
            eng, max_concurrent=args.max_concurrent,
            driver_echo=lambda line: print(line, file=sys.stderr, flush=True),
        )
        util_provider = _auto_util_provider() if args.util == "auto" else None
        result = sched.run(
            args.run, registry_runner(eng.registry), util_pct=util_pct,
            util_provider=util_provider, sleeper=time.sleep if args.wait else None,
            heartbeat_interval_s=args.heartbeat_interval,
        )
        _emit(result)
        # #313: stopping with orphaned dispatch leases we may not reclaim is NOT a
        # successful run — a status dump that looks like completion is exactly the silent
        # failure this exits non-zero for, so a wrapper/CI can branch on it.
        if result.get("scheduler", {}).get("exit_reason") == EXIT_BLOCKED_ORPHANED:
            print(result["scheduler"]["message"], file=sys.stderr)
            return 1
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
    elif args.cmd == "abandon":
        # args.disposition is an argparse Namespace attr (typed Any), so abandon()'s
        # Literal never bites at this call site. Narrow it to the Literal here — the
        # argparse choices=["failed", "rejected"] above is the runtime source of truth
        # that makes the cast sound — so mypy now flags a non-Literal disposition (#194).
        disposition = cast(Literal["failed", "rejected"], args.disposition)
        task = eng.abandon(args.run, args.task, reason=args.reason,
                           disposition=disposition, min_idle_s=args.min_idle_s,
                           force=args.force)
        _emit({"abandoned": task.task_id, "state": task.state.value,
               "disposition": disposition})
    elif args.cmd == "retire":
        run = eng.retire(args.run, reason=args.reason, retired_by=args.by,
                         superseded_by=args.superseded_by, force=args.force)
        progress = run.progress()
        _emit({"retired": run.run_id, "state": run.state.value,
               "superseded": progress.superseded, "superseded_by": run.superseded_by,
               "by": args.by, "reason": args.reason})
    elif args.cmd == "trunk-gate":
        result = eng.trunk_gate(args.run, cwd=args.cwd, file_fix=args.file_fix)
        _emit(result)
        # Non-zero on red so a human or CI wrapper can branch on the exit code.
        return 0 if result["green"] else 1
    elif args.cmd == "resume":
        _emit(eng.resume(args.run))
    elif args.cmd == "status":
        _emit(eng.status(args.run, check_spec=args.check_spec))
    elif args.cmd == "refresh-spec":
        _emit(eng.refresh_spec(args.run, args.task, force=args.force,
                               check_only=args.check))
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
        # #281 defense in depth: report over THIS run's rows only, so a shared/legacy
        # ledger holding other runs' rows can't inflate the breakdown.
        run_rows = eng.run_rows(args.run)
        if args.by_effort:
            # AC#4 (#167): a no-Python-required readable surface — render by_effort()'s
            # pipeline-ordered spend/retry/failure-rate table instead of dumping raw JSON.
            from .render import render_by_effort

            print(render_by_effort(args.run, eng.ledger.by_effort(rows=run_rows)))
        else:
            _emit(eng.ledger.analysis(rows=run_rows))
    elif args.cmd == "retrospective":
        _emit(eng.retrospective(args.run))
    else:  # pragma: no cover
        p.error(f"unknown command {args.cmd}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
