// Interactive x claude execution lane — the in-session Workflow shim (target.md §4).
//
// CONTRACT (do not regress):
//  - This runs INSIDE a Claude Code session via the Workflow tool. It has NO
//    filesystem access, so it does NOT persist anything. It calls agent() per
//    WorkItem and RETURNS an array of StageResult objects to the supervisor, which
//    persists them via `orchestrator record` (Bash).
//  - Resume granularity is therefore the DISPATCH BATCH: results persist only on
//    Workflow return. Keep `args.workItems` small so re-running one batch is cheap.
//  - The engine's capacity-derived dispatch limit is the BINDING concurrency; the
//    parallel()/agent() cap here is only a ceiling — never exceed args.dispatchLimit.
//  - Every result is lane-attributed interactive:claude so the cost ledger can never
//    see an unattributed call (closes as-built D6).
//  - TIMEOUTS: the sandbox has no clock or timers (Date.now/setTimeout unavailable),
//    so wi.timeout_s CANNOT be enforced here. The SUPERVISOR owns it: if a dispatch
//    visibly exceeds the WorkItem's timeout_s, stop waiting and record a StageResult
//    with status 'timeout' (see the skill's step 2) so the engine classifies TIMEOUT
//    and retries from the checkpoint instead of hanging the run.
//
// Input  (args): { workItems: WorkItem[], dispatchLimit: number }
// Output (return): StageResult[]  — the supervisor writes each to a temp file and
//                  runs `orchestrator record --result <file>`.

export const meta = {
  name: 'workflow-shim-interactive-claude',
  description: 'Dispatch a batch of engine WorkItems via in-session agent() and return StageResults',
  phases: [{ title: 'Dispatch' }],
}

phase('Dispatch')

// The Workflow runtime may hand `args` to the script as a JSON *string* rather than a
// parsed object; normalize so the shim reads work items either way (else it silently
// dispatches nothing and returns []).
const A = typeof args === 'string' ? JSON.parse(args) : (args || {})

const items = A.workItems || []
const ceiling = Math.max(1, Math.min(A.dispatchLimit || 1, items.length || 1))

// agent()'s schema validator rejects a top-level `$schema`/`$id` — it tries to resolve
// the meta-schema URI as a $ref and fails ("no schema with key or ref ..."). The engine's
// canonical stage schemas all carry `$schema`, so strip meta-keys before dispatch.
function sanitizeSchema(s) {
  if (!s || typeof s !== 'object') return s
  const { $schema, $id, ...rest } = s
  return rest
}

// Schema-enforced structured output per stage. agent({schema}) makes the model
// return JSON matching the stage's output contract; a null/empty result -> the
// engine records it as a schema_violation/failure (the codex full-validation
// tightening is the codex runner's job in Phase 4).
function toStageResult(wi, agentResult) {
  const usage = (agentResult && agentResult.usage) || {}
  const out = agentResult && agentResult.structured_output ? agentResult.structured_output : null
  return {
    schema_version: '1',
    work_item_id: wi.id,
    content_hash: wi.content_hash,
    run_id: wi.run_id,
    task_id: wi.task_id,
    stage: wi.stage,
    attempt: wi.attempt || 0,
    model: wi.model,
    status: agentResult && agentResult.error ? 'failure' : (out ? 'success' : 'schema_violation'),
    structured_output: out,
    raw_output: (agentResult && agentResult.raw) || null,
    error: (agentResult && agentResult.error) || null,
    lane_used: { execution_mode: 'interactive', provider: 'claude', invocation: `agent(model=${wi.model})` },
    token_usage: {
      input: usage.input_tokens || 0,
      output: usage.output_tokens || 0,
      cache_read: usage.cache_read_input_tokens || 0,
      cache_write: usage.cache_creation_input_tokens || 0,
    },
    cost_usd: null, // the engine prices authoritatively from its single model table
    completed_at: A.now, // the supervisor injects an ISO timestamp (sandbox has no clock)
  }
}

// Run the batch concurrently up to the engine-supplied ceiling (a true cap).
const thunks = items.map((wi) => async () => {
  try {
    const res = await agent(wi.prompt, {
      model: wi.model,
      agentType: wi.agent || undefined, // persona from the project roster
      schema: A.schemas ? sanitizeSchema(A.schemas[wi.schema_ref]) : undefined,
      label: `${wi.stage}:${wi.task_id}`,
    })
    // With a schema, agent() returns the validated object itself (the structured
    // output). Per-call token usage is NOT on that object; if the Workflow runtime
    // exposes usage (e.g. res.__usage or a sibling channel) wire it here, otherwise
    // it stays 0 and the engine prices 0 for this call. KNOWN LIMITATION: interactive
    // token usage may be uncapturable in-sandbox — the supervisor reconciles total
    // run cost from the session's own usage when exact per-stage cost is needed.
    return toStageResult(wi, { structured_output: res, usage: res && res.__usage })
  } catch (e) {
    return toStageResult(wi, { error: String((e && e.message) || e) })
  }
})

// Chunk to the ceiling so we never exceed the engine's capacity-derived limit.
const results = []
for (let i = 0; i < thunks.length; i += ceiling) {
  const slice = thunks.slice(i, i + ceiling)
  const batch = await parallel(slice)
  results.push(...batch.filter(Boolean))
}

return results
