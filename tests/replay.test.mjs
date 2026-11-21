import test from 'node:test'
import assert from 'node:assert/strict'

import { Orchestrator } from '../src/orchestrator.mjs'
import { MemoryEventLog } from '../src/event-log.mjs'
import { createServiceHarness, diamondPlan, CrashingLog } from '../src/testing.mjs'

/**
 * Reopen a run from a durable snapshot, optionally through a crashing log.
 * The service harness is *not* recreated, modelling an external backend that
 * survived the orchestrator process.
 */
function reopen({ plan, runId, snapshot, harness, crashOn, maxCompensationAttempts }) {
  const inner = MemoryEventLog.from(snapshot)
  const log = crashOn ? new CrashingLog(inner, crashOn) : inner
  const orchestrator = new Orchestrator({
    plan,
    runId,
    log,
    maxCompensationAttempts,
  })
  return { orchestrator, log, inner }
}

test('restart resumes an unconfirmed forward command with the same credential', async () => {
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)

  const firstLog = new MemoryEventLog()
  const crashing = new CrashingLog(
    firstLog,
    (event) => event.type === 'step.succeeded' && event.stepId === 'root',
  )
  const crashed = new Orchestrator({ plan, runId: 'resume-forward', log: crashing })

  await assert.rejects(crashed.run(), /crashed while recording/)
  assert.equal(crashing.crashed, true)

  // The forward call was applied, but its confirmation never reached the log.
  assert.equal(harness.appliedForwardCount('root'), 1)
  const snapshot = firstLog.snapshot()
  assert.ok(
    snapshot.some(
      (event) => event.type === 'step.dispatched' && event.stepId === 'root',
    ),
  )
  assert.ok(
    !snapshot.some(
      (event) => event.type === 'step.succeeded' && event.stepId === 'root',
    ),
  )

  const { orchestrator } = reopen({
    plan,
    runId: 'resume-forward',
    snapshot,
    harness,
  })
  const summary = await orchestrator.run()

  assert.equal(summary.status, 'completed')
  const rootCalls = harness.callsFor('root', 'forward')
  assert.equal(rootCalls.length, 2)
  assert.equal(new Set(rootCalls.map((call) => call.credential)).size, 1)
  assert.equal(rootCalls[1].deduped, true)
  assert.equal(harness.appliedForwardCount('root'), 1)
  assert.equal(harness.appliedCompensationCount('root'), 0)
})

test('restart resumes unconfirmed commands recorded before an abort was persisted', async () => {
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)
  harness.script('left', 'forward', { type: 'fail', message: 'left down' })
  harness.script('right', 'forward', { type: 'ok', delayMs: 15 })

  const firstLog = new MemoryEventLog()
  const crashing = new CrashingLog(
    firstLog,
    (event) => event.type === 'step.failed' && event.stepId === 'left',
  )
  await assert.rejects(
    new Orchestrator({ plan, runId: 'resume-abort', log: crashing }).run(),
    /crashed while recording/,
  )

  const snapshot = firstLog.snapshot()
  assert.ok(!snapshot.some((event) => event.type === 'run.aborted'))
  assert.ok(!snapshot.some((event) => event.type === 'step.failed'))
  assert.equal(harness.appliedForwardCount('right'), 1)

  const { orchestrator } = reopen({
    plan,
    runId: 'resume-abort',
    snapshot,
    harness,
  })
  const summary = await orchestrator.run()

  assert.equal(summary.status, 'compensated')
  assert.equal(summary.failedStepId, 'left')

  const leftCalls = harness.callsFor('left', 'forward')
  assert.equal(leftCalls.length, 2)
  assert.equal(new Set(leftCalls.map((call) => call.credential)).size, 1)

  const rightCalls = harness.callsFor('right', 'forward')
  assert.equal(rightCalls.length, 2)
  assert.equal(new Set(rightCalls.map((call) => call.credential)).size, 1)
  assert.equal(rightCalls[1].deduped, true)
  assert.equal(harness.appliedForwardCount('right'), 1)
  assert.equal(harness.appliedCompensationCount('right'), 1)
  assert.equal(harness.appliedCompensationCount('root'), 1)
  assert.equal(harness.callsFor('join', 'forward').length, 0)
})

test('restart during compensation resumes the unconfirmed compensation exactly once', async () => {
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)
  harness.script('join', 'forward', 'fail')

  const firstLog = new MemoryEventLog()
  const crashing = new CrashingLog(
    firstLog,
    (event) => event.type === 'step.compensated' && event.stepId === 'right',
  )
  await assert.rejects(
    new Orchestrator({ plan, runId: 'resume-comp', log: crashing }).run(),
    /crashed while recording/,
  )

  const snapshot = firstLog.snapshot()
  assert.ok(
    snapshot.some(
      (event) =>
        event.type === 'step.dispatched' &&
        event.stepId === 'right' &&
        event.phase === 'compensate',
    ),
  )
  assert.ok(!snapshot.some((event) => event.type === 'step.compensated'))
  assert.equal(harness.appliedCompensationCount('right'), 1)

  const { orchestrator } = reopen({
    plan,
    runId: 'resume-comp',
    snapshot,
    harness,
  })
  const summary = await orchestrator.run()

  assert.equal(summary.status, 'compensated')

  const rightCompensations = harness.callsFor('right', 'compensate')
  assert.equal(rightCompensations.length, 2)
  assert.equal(
    new Set(rightCompensations.map((call) => call.credential)).size,
    1,
  )
  assert.equal(rightCompensations[1].deduped, true)
  assert.equal(harness.appliedCompensationCount('right'), 1)

  const order = harness.calls
    .filter((call) => call.phase === 'compensate')
    .map((call) => call.stepId)
  assert.equal(order.at(-1), 'root')
})

test('a finished run is not re-executed on reopen', async () => {
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)

  const log = new MemoryEventLog()
  const first = new Orchestrator({ plan, runId: 'twice', log })
  const a = await first.run()
  const callsAfterFirst = harness.calls.length

  const second = new Orchestrator({ plan, runId: 'twice', log })
  const b = await second.run()

  assert.equal(b.status, 'completed')
  assert.deepEqual(b.steps, a.steps)
  assert.equal(harness.calls.length, callsAfterFirst)
  assert.equal(
    log.events().filter((event) => event.type === 'run.finished').length,
    1,
  )
})

test('replay is deterministic: projecting the same log twice yields equal state', async () => {
  const { projectEvents } = await import('../src/orchestrator.mjs')
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)
  harness.script('left', 'forward', 'fail')

  const log = new MemoryEventLog()
  await new Orchestrator({ plan, runId: 'projection', log }).run()
  const events = log.snapshot()

  const one = projectEvents(events, plan)
  const two = projectEvents(structuredClone(events), plan)

  const flatten = (state) => ({
    aborted: state.aborted,
    failedStepId: state.failedStepId,
    forward: [...state.forward].map(([id, value]) => [id, value.state]),
    compensation: [...state.compensation].map(([id, value]) => [id, value.state]),
    finished: state.finished?.status,
  })
  assert.deepEqual(flatten(one), flatten(two))
  assert.equal(one.finished.status, 'compensated')
})

test('reopening with a mismatched runId or plan is refused', async () => {
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)

  const log = new MemoryEventLog()
  await new Orchestrator({ plan, runId: 'original', log }).run()

  await assert.rejects(
    new Orchestrator({ plan, runId: 'different-run', log }).run(),
    /belongs to run "original"/,
  )

  const otherPlan = diamondPlan(harness.services, { join: false })
  await assert.rejects(
    new Orchestrator({ plan: otherPlan, runId: 'original', log }).run(),
    /different plan/,
  )
})

test('a crash between repeated compensation attempts resumes without exceeding the limit', async () => {
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)
  harness.script('join', 'forward', 'fail')
  harness.script('root', 'compensate', { type: 'fail', message: 'flaky' }, 'ok')

  const firstLog = new MemoryEventLog()
  const crashing = new CrashingLog(
    firstLog,
    (event) =>
      event.type === 'step.compensation_failed' && event.stepId === 'root',
  )
  await assert.rejects(
    new Orchestrator({
      plan,
      runId: 'retry-crash',
      log: crashing,
      maxCompensationAttempts: 3,
    }).run(),
    /crashed while recording/,
  )

  const { orchestrator } = reopen({
    plan,
    runId: 'retry-crash',
    snapshot: firstLog.snapshot(),
    harness,
    maxCompensationAttempts: 3,
  })
  const summary = await orchestrator.run()

  assert.equal(summary.status, 'compensated')
  const rootAttempts = harness.callsFor('root', 'compensate')
  assert.equal(new Set(rootAttempts.map((call) => call.credential)).size, 1)
  // The crash happened before the failure was recorded, so the resent command
  // is still attempt 1 of the same logical try; the limit counts recorded
  // failures, not process restarts.
  assert.deepEqual(rootAttempts.map((call) => call.attempt), [1, 1])
  assert.equal(harness.appliedCompensationCount('root'), 1)
})

test('maxCompensationAttempts = 1 abandons after a single failed compensation', async () => {
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)
  harness.script('join', 'forward', 'fail')
  harness.script('left', 'compensate', { type: 'fail', message: 'nope' })

  const summary = await new Orchestrator({
    plan,
    runId: 'single-attempt',
    log: new MemoryEventLog(),
    maxCompensationAttempts: 1,
  }).run()

  assert.equal(harness.callsFor('left', 'compensate').length, 1)
  const left = summary.steps.find((step) => step.id === 'left')
  assert.equal(left.compensation, 'abandoned')
  assert.equal(summary.status, 'compensation_incomplete')
})
