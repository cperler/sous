# Fragment: <file(s)>
Source commit: <sha>   Mapped lines: <range(s)>

## 1. Role & entry points — who invokes it, with what argv
## 2. Inputs — every flag, env var (name / default / effect), file read
## 3. Outputs — every file written (path, format, EVERY field), exit codes, side effects (git, gh, network)
## 4. Control flow — state machine: states, transitions, loop structure with exact caps and exit conditions
## 5. External invocations — every claude/codex/gh/git command VERBATIM with flags, model, schema
## 6. Constants & tunables — numeric caps, timeouts, sleeps, pricing, model pins
## 7. Failure handling — retries (count/backoff), fallback chains, circuit breaker, cascade rules
## 8. Coupling — per item: generic vs Hey Soo!-specific (with the generic shape it should take)
## 9. Anomalies — suspected bugs, dead code, contradictions with docs/orchestration-template.md

> Hard rule: every claim in §§4–7 cites `absolute-path:line`.
