// Test driver for run_targets/workflow_shim.js — exercises the shim's runtime-hardening
// behaviors OUTSIDE the Workflow runtime:
//   1) `args` delivered as a JSON *string* (not an object) is still parsed.
//   2) a stage schema carrying a top-level `$schema`/`$id` is sanitized before
//      it reaches agent() (whose validator rejects the meta-schema URI as a $ref).
//   3) (#311, mode `badhash`) a WorkItem whose content_hash is not a 64-char sha256
//      hexdigest aborts the batch before any agent() call is made.
//
// Usage: node _shim_driver.mjs <shim path> [badhash]
//
// The shim is authored as a Workflow script (module-level `phase()/agent()/parallel()`
// and a top-level `return`), so it can't be imported directly. We strip the lone
// `export` and evaluate the body inside an async Function with the runtime hooks stubbed.
import { readFileSync } from 'node:fs'

const shimPath = process.argv[2]
const mode = process.argv[3] || 'ok'
const src = readFileSync(shimPath, 'utf8').replace(/^export\s+const\s+meta/m, 'const meta')

const capturedSchemas = []
const capturedEffort = {} // prompt -> the `effort` opt agent() received (#96)
const phase = () => {}
const log = () => {}
const parallel = async (thunks) => Promise.all(thunks.map((t) => t()))
const agent = async (prompt, opts) => {
  capturedSchemas.push(opts.schema)
  capturedEffort[prompt] = 'effort' in opts ? opts.effort : '<absent>'
  return { ok: true }
}

const argsObj = {
  dispatchLimit: 2,
  now: 'T',
  schemas: {
    implement: {
      $schema: 'https://json-schema.org/draft/2020-12/schema',
      $id: 'urn:x',
      type: 'object',
      properties: { committed: { type: 'boolean' } },
    },
  },
  workItems: [
    // wi-1 carries a #96 effort; wi-2 is effort-less (pre-#96 shape) — the shim must
    // pass the former to agent() and keep the latter's opts/result effort-free.
    // The hashes are real-shaped sha256 hexdigests: #311 made the shim refuse anything
    // that is not one, so a placeholder like 'h1' would (correctly) abort the batch.
    { id: 'wi-1', content_hash: 'a'.repeat(64), run_id: 'r', task_id: '#1', stage: 'implement', attempt: 0, model: 'claude-opus-5', schema_ref: 'implement', prompt: 'p1', effort: 'high' },
    { id: 'wi-2', content_hash: 'b'.repeat(64), run_id: 'r', task_id: '#1', stage: 'implement', attempt: 0, model: 'claude-opus-5', schema_ref: 'implement', prompt: 'p2' },
  ],
}

// #311: the exact live failure — a 16-char log preview pasted over the full digest.
if (mode === 'badhash') {
  argsObj.workItems[1].content_hash = argsObj.workItems[1].content_hash.slice(0, 16)
}
// Deliver args as a STRING, exactly as the Workflow runtime was observed to.
const argsString = JSON.stringify(argsObj)

const fn = new Function(
  'args', 'phase', 'log', 'agent', 'parallel',
  '"use strict"; return (async () => {\n' + src + '\n})()',
)
let results
try {
  results = await fn(argsString, phase, log, agent, parallel)
} catch (e) {
  // The #311 guard aborts the batch by throwing. Report the refusal (and, crucially, that
  // no agent() call was made) as data so the Python test can assert on it.
  console.log(JSON.stringify({
    threw: String((e && e.message) || e),
    agentCalls: capturedSchemas.length,
  }))
  process.exit(0)
}

console.log(JSON.stringify({
  resultCount: results.length,
  statuses: results.map((r) => r.status),
  completedAt: results.map((r) => r.completed_at),
  schemaKeys: capturedSchemas.map((s) => (s ? Object.keys(s) : null)),
  agentEffort: capturedEffort, // prompt -> effort opt (undefined serializes as absent)
  resultEffort: Object.fromEntries(results.map((r) => [r.work_item_id, r.effort])),
}))
