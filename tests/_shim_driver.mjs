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
          // U+1C89/U+1C8A acquired a case pair after fingerprint-v1's Unicode-15
          // table. They must remain distinct even when Node knows the newer mapping.
          { severity: 'critical', file: '\u1c89.py', line: 3, description: 'PINNED' },
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
          { severity: 'critical', file: '\u1c8a.py', line: 4, description: 'pinned' },
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
        let fingerprint
        if (prompt.includes('strasse.py:broken')) fingerprint = 'strasse.py:broken'
        else if (prompt.includes('\u1c89.py:pinned')) fingerprint = '\u1c89.py:pinned'
        else if (prompt.includes('\u1c8a.py:pinned')) fingerprint = '\u1c8a.py:pinned'
        else fingerprint = `:${'a'.repeat(158)}😀`
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

// An unknown dedupe rule is echoed verbatim into the `unknown_dedupe_rule` notice, which is
// the one notice detail a plan can push past the 200-code-point cap. Astral characters ahead
// of that boundary shift the UTF-16 offsets away from the code-point offsets, so a UTF-16
// `slice` truncates different text than the engine's Python slicing — and can cut an emoji in
// half, emitting a lone surrogate.
if (mode === 'panel-notice-astral') {
  argsObj.workItems[0].plan.dedupe_rule = `x${'😀'.repeat(100)}${'y'.repeat(100)}`
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
