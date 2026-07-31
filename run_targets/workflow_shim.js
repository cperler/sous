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
//  - PLAN EXECUTION (#262): a plan-bearing REVIEW fans out below the WorkItem seam into
//    blind finders and adversarial verifiers, then returns the same `sub_results` /
//    `sub_calls` contract as the headless x claude driver. The engine owns the deterministic
//    fold into review.json; this shim never synthesizes a verdict. Keep this branch in
//    conformance with adapters/execution/review_panel.py — `supports_plan: true` relies on it.
//  - WORKITEM FIDELITY (#311): every WorkItem must reach this shim byte-for-byte as the
//    engine emitted it — `content_hash` is echoed onto the StageResult and is what ties
//    the result to its dispatch. A malformed hash aborts the batch BEFORE any agent()
//    call (see the HEX64 guard below), so a transcription slip costs zero model spend.
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

// #311: the supervisor hand-assembles this args payload, so a WorkItem field can be
// mistyped — and `content_hash` (a sha256 hexdigest, always 64 lowercase hex chars) is the
// one field whose whole job is tying the result back to the dispatch. A truncated paste (a
// 16-char log preview) was echoed back on two stages of a live run. Check the SHAPE before
// any agent() call: the engine's record() would refuse the echoed result anyway, but by
// then the stage has already been paid for. Fail the whole batch loudly instead — nothing
// here is recoverable in-sandbox (the shim has no filesystem and cannot re-read the real
// WorkItem), so the supervisor must re-dispatch with the WorkItem passed through verbatim.
const HEX64 = /^[0-9a-f]{64}$/
const malformed = items.filter((wi) => !HEX64.test(String((wi && wi.content_hash) || '')))
if (malformed.length) {
  const detail = malformed
    .map((wi) => `${wi.id}: ${JSON.stringify(wi.content_hash)}`)
    .join(', ')
  throw new Error(
    `workflow_shim: ${malformed.length} work item(s) carry a malformed content_hash ` +
    `(expected a 64-char sha256 hexdigest) — ${detail}. Nothing was dispatched. Re-run ` +
    'with each WorkItem copied VERBATIM from `orchestrator next` (never retyped, never a ' +
    'truncated preview, never mixed between in-flight items).',
  )
}

// agent()'s schema validator rejects a top-level `$schema`/`$id` — it tries to resolve
// the meta-schema URI as a $ref and fails ("no schema with key or ref ..."). The engine's
// canonical stage schemas all carry `$schema`, so strip meta-keys before dispatch.
function sanitizeSchema(s) {
  if (!s || typeof s !== 'object') return s
  const { $schema, $id, ...rest } = s
  return rest
}

// Contract constants mirrored from adapters/execution/review_panel.py and
// orchestrator/review_workflow.py. The Node harness drives both implementations over the
// same plan so a drift in ordering, capping, prompt filling, or failure direction is caught.
const FINGERPRINT_RULE = 'fingerprint-v1'
const LENS_ORDER = ['find:code', 'find:spec', 'find:design', 'find:tests']
const MAX_VERIFIERS = 8
const MAX_NOTICE_DETAIL = 200

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function tokenUsage(usage) {
  const u = isRecord(usage) ? usage : {}
  return {
    input: u.input_tokens || u.input || 0,
    output: u.output_tokens || u.output || 0,
    cache_read: u.cache_read_input_tokens || u.cache_read || 0,
    cache_write: u.cache_creation_input_tokens || u.cache_write || 0,
  }
}

function sumUsage(subCalls) {
  return subCalls.reduce((total, call) => ({
    input: total.input + call.usage.input,
    output: total.output + call.usage.output,
    cache_read: total.cache_read + call.usage.cache_read,
    cache_write: total.cache_write + call.usage.cache_write,
  }), { input: 0, output: 0, cache_read: 0, cache_write: 0 })
}

function classifyAgentError(error) {
  const message = String(error || '')
  if (/timed?\s*out|timeout/i.test(message)) return 'timeout'
  if (/\b429\b|rate[ _-]*limit/i.test(message)) return 'rate_limited'
  return 'failure'
}

/**
 * Run one panel sub-call without letting a provider error abort sibling attribution.
 *
 * The returned shape deliberately separates a structured object from an error: finder
 * failures terminate the panel, while verifier failures are retained as inconclusive evidence
 * so the engine's fold leaves the finding blocking. `phaseName` is audit-only; the parent
 * WorkItem remains the single engine-visible dispatch.
 */
async function dispatchSubCall(wi, phaseName, prompt, schemaRef, agentType) {
  try {
    const res = await agent(prompt, {
      model: wi.model,
      effort: wi.effort || undefined,
      agentType: agentType || undefined,
      schema: A.schemas ? sanitizeSchema(A.schemas[schemaRef]) : undefined,
      label: `${wi.stage}:${wi.task_id}:${phaseName}`,
    })
    return { output: isRecord(res) ? res : null, usage: res && res.__usage, error: null }
  } catch (e) {
    return { output: null, usage: null, error: String((e && e.message) || e) }
  }
}

function subCall(phaseName, wi, call) {
  return {
    phase: phaseName,
    model: wi.model,
    usage: tokenUsage(call.usage),
    // The Workflow sandbox exposes no clock. Zero is an honest unavailable duration; the
    // per-call usage flag below separately prevents an unavailable usage report reading $0.
    duration_s: 0,
    session_id: null,
    stream_file: null,
    schema_retries: 0,
    usage_recovered: isRecord(call.usage),
  }
}

function issueFingerprint(issue) {
  const base = isRecord(issue)
    ? `${String(issue.file || '').trim()}:${String(issue.description || '').trim()}`
    : String(issue)
  // JavaScript has no casefold primitive; lower-casing is its direct analogue for the
  // model-authored file/description text used by fingerprint-v1.
  return base.replace(/\s+/gu, ' ').toLowerCase().slice(0, 160)
}

function findingsOf(payload) {
  return isRecord(payload) && Array.isArray(payload.findings) ? payload.findings : []
}

function lensWalk(findingsByLens) {
  const unknown = Object.keys(findingsByLens)
    .filter((lens) => !LENS_ORDER.includes(lens))
    .sort()
  return [...LENS_ORDER.filter((lens) => lens in findingsByLens), ...unknown]
}

function isBlocking(finding) {
  if (!isRecord(finding) || finding.severity === null || finding.severity === undefined) {
    return true
  }
  return String(finding.severity).trim().toLowerCase() !== 'suggestion'
}

function severityRank(finding) {
  if (!isRecord(finding)) return 1
  const severity = String(finding.severity || '').trim().toLowerCase()
  return severity === 'critical' ? 0 : 1
}

function notice(kind, detail, extras = {}) {
  const bounded = detail.length <= MAX_NOTICE_DETAIL
    ? detail
    : `${detail.slice(0, MAX_NOTICE_DETAIL)} … [truncated]`
  return { notice: kind, detail: bounded, ...extras }
}

function verifyQueue(findingsByLens, dedupeRule) {
  const notices = []
  const dedupe = dedupeRule === FINGERPRINT_RULE
  if (!dedupe) {
    notices.push(notice(
      'unknown_dedupe_rule',
      `plan dedupe_rule ${JSON.stringify(dedupeRule)} is not ${JSON.stringify(FINGERPRINT_RULE)}` +
      ' — findings were NOT deduped before verification (the engine still dedupes at synthesis)',
    ))
  }
  const seen = new Set()
  const queue = []
  for (const lens of lensWalk(findingsByLens)) {
    for (const finding of findingsOf(findingsByLens[lens])) {
      const fingerprint = issueFingerprint(finding)
      if (dedupe && seen.has(fingerprint)) continue
      if (dedupe) seen.add(fingerprint)
      if (isBlocking(finding)) queue.push([severityRank(finding), fingerprint, finding])
    }
  }
  queue.sort((a, b) => a[0] - b[0] || (a[1] < b[1] ? -1 : (a[1] > b[1] ? 1 : 0)))
  return { queue: queue.map(([, fingerprint, finding]) => [fingerprint, finding]), notices }
}

function findingBlock(fingerprint, finding) {
  if (!isRecord(finding)) return `- fingerprint: ${fingerprint}\n- description: ${finding}`
  const lines = [`- fingerprint: ${fingerprint}`]
  for (const key of ['severity', 'file', 'line', 'description', 'suggested_fix']) {
    const value = finding[key]
    if (value !== null && value !== undefined && String(value).trim()) {
      lines.push(`- ${key}: ${value}`)
    }
  }
  return lines.join('\n')
}

function diffHint(finding) {
  if (isRecord(finding)) {
    const file = String(finding.file || '').trim()
    if (file && Number.isInteger(finding.line)) return `${file}:${finding.line}`
    if (file) return file
  }
  return 'the change under review in this working tree'
}

function verifyPrompt(template, fingerprint, finding) {
  const values = {
    finding: findingBlock(fingerprint, finding),
    diff_hint: diffHint(finding),
  }
  // One replacement pass: a literal slot name inside model-authored finding text must not
  // be visited again and rewritten by a later replacement.
  return template.replace(/\{(finding|diff_hint)\}/g, (_, slot) => values[slot])
}

function verifierVerdict(call, fingerprint) {
  if (call.error) return [null, `verifier ${classifyAgentError(call.error)}: ${call.error.slice(0, 120)}`]
  if (!isRecord(call.output)) return [null, 'verifier schema_violation: no structured output']
  const echoed = call.output.fingerprint
  if (typeof echoed !== 'string' || issueFingerprint(echoed) !== fingerprint) {
    return [null, `verifier echoed an unmatchable fingerprint ${JSON.stringify(String(echoed).slice(0, 80))}`]
  }
  return [{
    fingerprint,
    verdict: call.output.verdict,
    reasoning: call.output.reasoning,
  }, '']
}

/**
 * Build the one StageResult that represents an entire review panel.
 *
 * Provider usage is summed only for the dispatch-level view. Every individual call remains in
 * `sub_calls` for ledger attribution; unavailable Workflow usage is marked unrecovered rather
 * than presented as a metered zero-cost call.
 */
function panelResult(wi, status, subCalls, extras = {}) {
  const usage = sumUsage(subCalls)
  return {
    ...toStageResult(wi, { structured_output: null, usage: null }),
    status,
    structured_output: null,
    token_usage: usage,
    usage_recovered: subCalls.every((call) => call.usage_recovered),
    schema_retries: subCalls.reduce((total, call) => total + call.schema_retries, 0),
    sub_calls: subCalls,
    ...extras,
  }
}

/**
 * Execute a ReviewPlan and return its raw, unfolded panel evidence as one StageResult.
 *
 * Finders run in plan order and fail the dispatch as a whole, because a missing lens is a
 * missing review. Blocking findings are deduped in the fold's lens order, capped, and sent to
 * independent verifiers. Verifier failure never removes scrutiny: it produces a notice and no
 * verdict. This function intentionally does not synthesize review.json; `orchestrator record`
 * owns the shared deterministic fold for both execution lanes.
 */
async function runReviewPanel(wi) {
  const plan = wi.plan
  const notices = []
  const calls = []
  const findingsByLens = {}

  // Sequential like the headless reference: every sub-agent shares one working tree, and a
  // missing finder short-circuits before another lens or any verifier can spend money.
  for (const finder of plan.finders) {
    const call = await dispatchSubCall(
      wi, finder.lens, finder.prompt, finder.schema_ref, finder.agent,
    )
    calls.push(subCall(finder.lens, wi, call))
    if (call.error || !isRecord(call.output)) {
      const status = call.error ? classifyAgentError(call.error) : 'schema_violation'
      const problem = call.error || 'no structured output'
      return panelResult(wi, status, calls, {
        error: `review panel finder ${finder.lens} failed: ${problem}`.slice(0, 500),
        raw_output: null,
        sub_results: null,
        lane_used: {
          execution_mode: 'interactive', provider: 'claude',
          invocation: `review panel finder ${finder.lens}`,
        },
      })
    }
    findingsByLens[finder.lens] = call.output
  }

  let queued = verifyQueue(findingsByLens, plan.dedupe_rule)
  notices.push(...queued.notices)
  if (queued.queue.length > MAX_VERIFIERS) {
    const dropped = queued.queue.slice(MAX_VERIFIERS)
    notices.push(notice(
      'verifier_cap',
      `${queued.queue.length} blocking findings exceed the ${MAX_VERIFIERS}-verifier cap — ` +
      `${dropped.length} unverified (they stay BLOCKING): ` +
      dropped.map(([fingerprint]) => fingerprint).join(', '),
      { count: dropped.length },
    ))
    queued = { ...queued, queue: queued.queue.slice(0, MAX_VERIFIERS) }
  }

  const verdicts = []
  for (let index = 0; index < queued.queue.length; index += 1) {
    const [fingerprint, finding] = queued.queue[index]
    const phaseName = `verify:${index + 1}`
    const call = await dispatchSubCall(
      wi, phaseName, verifyPrompt(plan.verify_template, fingerprint, finding),
      plan.verify_schema_ref, null,
    )
    calls.push(subCall(phaseName, wi, call))
    const [verdict, problem] = verifierVerdict(call, fingerprint)
    if (verdict === null) {
      notices.push(notice(
        'verifier_inconclusive', `${phaseName} on ${fingerprint}: ${problem} — stays blocking`,
      ))
    } else {
      verdicts.push(verdict)
    }
  }

  const findingCount = Object.values(findingsByLens)
    .reduce((total, payload) => total + findingsOf(payload).length, 0)
  const verifierCount = calls.filter((call) => call.phase.startsWith('verify:')).length
  return panelResult(wi, 'success', calls, {
    raw_output: `[review panel] ${Object.keys(findingsByLens).length} finder(s), ` +
      `${verifierCount} verifier(s); ${findingCount} finding(s), ${verdicts.length} verdict(s). ` +
      "The engine's fold owns review.json.",
    error: null,
    sub_results: {
      findings_by_lens: findingsByLens,
      verdicts,
      notices,
    },
    lane_used: {
      execution_mode: 'interactive', provider: 'claude',
      invocation: `review panel (${calls.length} sub-calls)`,
    },
  })
}

// Schema-enforced structured output per stage. agent({schema}) makes the model
// return JSON matching the stage's output contract; a null/empty result -> the
// engine records it as a schema_violation/failure (the codex full-validation
// tightening is the codex runner's job in Phase 4).
function toStageResult(wi, agentResult) {
  const usage = (agentResult && agentResult.usage) || {}
  const out = agentResult && agentResult.structured_output ? agentResult.structured_output : null
  return {
    // #275: ECHO the dispatching WorkItem's version rather than restating one. This shim
    // hardcoded '1' while the engine had moved to "3", and nothing caught it because the
    // engine ignored the field entirely; it now refuses an off-version result at record().
    // Echoing means this lane cannot drift again — the engine that emitted the WorkItem is
    // by definition the engine that will record the result. The literal is only a fallback
    // for a WorkItem assembled without the field, and tests/test_schema_compat.py pins it
    // to SCHEMA_VERSION so it cannot silently rot the way '1' did.
    schema_version: wi.schema_version || '3',
    work_item_id: wi.id,
    content_hash: wi.content_hash,
    run_id: wi.run_id,
    task_id: wi.task_id,
    stage: wi.stage,
    attempt: wi.attempt || 0,
    model: wi.model,
    effort: wi.effort || null, // #96: echoed for the ledger row / stage events (audit)
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
  if (wi.plan) return runReviewPanel(wi)
  try {
    // #302 (decided): `wi.tool_policy` / `wi.permission_posture` are NOT passed below —
    // agent() exposes no tool-restriction or permission option, so this cell declares
    // `enforces_tool_policy=false` rather than pretend. The engine compensates by stating the
    // posture in-band inside `wi.prompt` for this lane, plus a `tool_policy_unenforced` event
    // per dispatch. If agent() ever gains a tool option, wire it HERE and flip the flag on the
    // interactive×claude descriptor in the same change (which also retires the directive).
    const res = await agent(wi.prompt, {
      model: wi.model,
      effort: wi.effort || undefined, // #96: per-stage reasoning effort (low/medium/high)
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
