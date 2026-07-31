// Test driver for run_targets/workflow_shim.js — exercises the shim's runtime-hardening
// behaviors OUTSIDE the Workflow runtime:
//   1) `args` delivered as a JSON *string* (not an object) is still parsed.
//   2) a stage schema carrying a top-level `$schema`/`$id` is sanitized before
//      it reaches agent() (whose validator rejects the meta-schema URI as a $ref).
//   3) (#311, mode `badhash`) a WorkItem whose content_hash is not a 64-char sha256
//      hexdigest aborts the batch before any agent() call is made.
//   4) (#262, `panel*` modes) a ReviewPlan takes the finder/verifier branch and returns
//      its unfolded panel contract rather than the ordinary single-reviewer result.
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
const capturedCalls = []
const phase = () => {}
const log = () => {}
const parallel = async (thunks) => Promise.all(thunks.map((t) => t()))
const agent = async (prompt, opts) => {
  capturedSchemas.push(opts.schema)
  capturedEffort[prompt] = 'effort' in opts ? opts.effort : '<absent>'
  capturedCalls.push({ prompt, agentType: opts.agentType || null, label: opts.label })
  if (mode.startsWith('panel')) {
    if (prompt === 'finder-code') {
      if (mode === 'panel-unicode') {
        return { findings: [
          { severity: 'critical', file: 'Straße.py', line: 1, description: 'BROKEN' },
          { severity: 'important', file: '', line: 2,
            description: `${'a'.repeat(158)}😀discarded` },
        ] }
      }
      if (mode === 'panel-cap') {
        return { findings: Array.from({ length: 12 }, (_, i) => ({
          severity: 'critical', file: 'cap.py', line: i + 1,
          description: `bug number ${String(i).padStart(2, '0')}`,
        })) }
      }
      return { findings: [{ severity: 'critical', file: 'a.py', line: 7,
        description: 'Null deref in the guard' }] }
    }
    if (prompt === 'finder-spec') {
      if (mode === 'panel-finder-error') throw new Error('finder exploded')
      if (mode === 'panel-cap') return { findings: [] }
      if (mode === 'panel-unicode') {
        return { findings: [
          { severity: 'critical', file: 'STRASSE.PY', line: 99, description: 'broken' },
        ] }
      }
      return { findings: [
        { severity: 'critical', file: 'a.py', line: 99,
          description: 'null   DEREF in the guard' },
        { severity: 'suggestion', file: 'b.py', line: 2, description: 'rename this' },
      ] }
    }
    if (prompt.startsWith('VERIFY ')) {
      if (mode === 'panel-verifier-error') throw new Error('verifier exploded')
      if (mode === 'panel-unicode') {
        const fingerprint = prompt.includes('strasse.py:broken')
          ? 'strasse.py:broken'
          : `:${'a'.repeat(158)}😀`
        return { fingerprint, verdict: 'confirmed', reasoning: 'confirmed across lanes' }
      }
      return {
        fingerprint: 'a.py:null deref in the guard',
        verdict: 'refuted',
        reasoning: 'guarded by the caller',
      }
    }
    throw new Error(`unexpected panel prompt: ${prompt}`)
  }
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
    review_findings: {
      $schema: 'https://json-schema.org/draft/2020-12/schema',
      type: 'object',
      properties: { findings: { type: 'array' } },
    },
    review_verdict: {
      $schema: 'https://json-schema.org/draft/2020-12/schema',
      type: 'object',
      properties: { fingerprint: { type: 'string' } },
    },
  },
  workItems: [
    // wi-1 carries a #96 effort; wi-2 is effort-less (pre-#96 shape) — the shim must
    // pass the former to agent() and keep the latter's opts/result effort-free.
    // The hashes are real-shaped sha256 hexdigests: #311 made the shim refuse anything
    // that is not one, so a placeholder like 'h1' would (correctly) abort the batch.
    // wi-1 also carries a deliberately non-current `schema_version` (#275): the shim must
    // ECHO the dispatching WorkItem's version rather than restate a literal of its own, so
    // an arbitrary value proves the echo where '3' would also match the fallback.
    { id: 'wi-1', schema_version: '77', content_hash: 'a'.repeat(64), run_id: 'r', task_id: '#1', stage: 'implement', attempt: 0, model: 'claude-opus-5', schema_ref: 'implement', prompt: 'p1', effort: 'high' },
    { id: 'wi-2', content_hash: 'b'.repeat(64), run_id: 'r', task_id: '#1', stage: 'implement', attempt: 0, model: 'claude-opus-5', schema_ref: 'implement', prompt: 'p2' },
  ],
}

if (mode.startsWith('panel')) {
  argsObj.dispatchLimit = 1
  argsObj.workItems = [{
    id: 'wi-panel', schema_version: '3', content_hash: 'c'.repeat(64), run_id: 'r',
    task_id: '#1', stage: 'review', attempt: 0, model: 'claude-opus-5',
    schema_ref: 'review', prompt: 'ordinary single-reviewer prompt', effort: 'high',
    plan: {
      finders: [
        { lens: 'find:code', prompt: 'finder-code', agent: 'code-reviewer',
          schema_ref: 'review_findings' },
        { lens: 'find:spec', prompt: 'finder-spec', agent: 'spec-reviewer',
          schema_ref: 'review_findings' },
      ],
      verify_template: 'VERIFY {finding}\nAT {diff_hint}',
      verify_schema_ref: 'review_verdict',
      dedupe_rule: 'fingerprint-v1',
    },
  }]
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

const output = {
  resultCount: results.length,
  statuses: results.map((r) => r.status),
  completedAt: results.map((r) => r.completed_at),
  schemaKeys: capturedSchemas.map((s) => (s ? Object.keys(s) : null)),
  agentEffort: capturedEffort, // prompt -> effort opt (undefined serializes as absent)
  resultEffort: Object.fromEntries(results.map((r) => [r.work_item_id, r.effort])),
  // #275: wi-1 carries schema_version '77' (echoed), wi-2 carries none (fallback literal).
  resultSchemaVersion: Object.fromEntries(results.map((r) => [r.work_item_id, r.schema_version])),
}
if (mode.startsWith('panel')) {
  output.panelResult = results[0]
  output.agentCallsDetailed = capturedCalls
}
console.log(JSON.stringify(output))
