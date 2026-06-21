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

const items = args.workItems || []
const ceiling = Math.max(1, Math.min(args.dispatchLimit || 1, items.length || 1))

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
    completed_at: args.now, // the supervisor injects an ISO timestamp (sandbox has no clock)
  }
}

// Run the batch concurrently up to the engine-supplied ceiling (a true cap).
const thunks = items.map((wi) => async () => {
  try {
    const res = await agent(wi.prompt, {
      model: wi.model,
      schema: args.schemas ? args.schemas[wi.schema_ref] : undefined,
      label: `${wi.stage}:${wi.task_id}`,
    })
    return toStageResult(wi, { structured_output: res, usage: res && res.usage })
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
