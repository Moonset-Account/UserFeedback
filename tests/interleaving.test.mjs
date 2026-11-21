import test from 'node:test'
import assert from 'node:assert/strict'

import { Orchestrator } from '../src/orchestrator.mjs'
import { MemoryEventLog } from '../src/event-log.mjs'
import { createServiceHarness } from '../src/testing.mjs'

/**
 * Interleavings: failures and late successes arriving out of order, and
 * timestamps proving the actual causal order of dispatches.
 */
function eventsOfType(log, type) {
  return log.events().filter((event) => event.type === type)
}

test('left fails fast, right reports success late: shared prerequisite waits for right to be undone', async () => {
  const harness = createServiceHarness()
  const plan = {
    services: harness.services,
    steps: [
      { id: 'root', service: 'storage' },
      { id: 'left', service: 'network', dependsOn: ['root'] },
      { id: 'right', service: 'compute', dependsOn: ['root'] },
    ],
  }
  harness.script('left', 'forward', { type: 'fail', message: 'left boom', delayMs: 1 })
  harness.script('right', 'forward', { type: 'ok', delayMs: 25 })
  harness.script('right', 'compensate', 'ok')

  const log = new MemoryEventLog()
  const summary = await new Orchestrator({ plan, runId: 'late-right', log }).run()

  assert.equal(summary.status, 'compensated')
  assert.equal(summary.failedStepId, 'left')

  // The failure is recorded before the late success.
  const forwardOutcomes = log
    .events()
    .filter((event) => event.type === 'step.failed' || event.type === 'step.succeeded')
    .map((event) => [event.type, event.stepId])
  assert.deepEqual(forwardOutcomes, [
    ['step.succeeded', 'root'],
    ['step.failed', 'left'],
    ['step.succeeded', 'right'],
  ])

  // root's compensation is dispatched only after right's compensation is
  // confirmed, even though right succeeded after the abort.
  const compensatedOrder = eventsOfType(log, 'step.compensated').map(
    (event) => event.stepId,
  )
  assert.deepEqual(compensatedOrder, ['right', 'root'])

  const rightConfirmedAt = log
    .events()
    .findIndex((event) => event.type === 'step.compensated' && event.stepId === 'right')
  const rootDispatchedAt = log
    .events()
    .findIndex(
      (event) =>
        event.type === 'step.dispatched' &&
        event.stepId === 'root' &&
        event.phase === 'compensate',
    )
  assert.ok(rightConfirmedAt < rootDispatchedAt)
  assert.equal(harness.appliedCompensationCount('root'), 1)
})

test('a late success in the failing wave is still compensated and gates its parent', async () => {
  const harness = createServiceHarness()
  const plan = {
    services: harness.services,
    steps: [
      { id: 'root', service: 'svc' },
      { id: 'slow-chain', service: 'svc', dependsOn: ['root'] },
      { id: 'deep', service: 'svc', dependsOn: ['slow-chain'] },
      { id: 'boom', service: 'svc', dependsOn: ['root'] },
    ],
  }
  // slow-chain and boom are dispatched in the same wave; boom fails, but
  // slow-chain's success arrives afterwards and must still be honoured.
  harness.script('boom', 'forward', { type: 'fail', message: 'boom', delayMs: 5 })
  harness.script('slow-chain', 'forward', { type: 'ok', delayMs: 30 })

  const log = new MemoryEventLog()
  const summary = await new Orchestrator({ plan, runId: 'late-wave', log }).run()

  assert.equal(summary.failedStepId, 'boom')
  const byId = Object.fromEntries(summary.steps.map((step) => [step.id, step]))

  // The in-flight sibling reached a conclusion and was compensated.
  assert.equal(byId['slow-chain'].forward, 'succeeded')
  assert.equal(byId['slow-chain'].compensation, 'compensated')

  // Its dependent was never started because the abort landed first.
  assert.equal(byId.deep.forward, 'pending')
  assert.equal(harness.callsFor('deep', 'forward').length, 0)

  // root waits for slow-chain's compensation before being undone.
  const compensatedOrder = eventsOfType(log, 'step.compensated').map(
    (event) => event.stepId,
  )
  assert.deepEqual(compensatedOrder, ['slow-chain', 'root'])

  const slowConfirmedAt = log
    .events()
    .findIndex(
      (event) => event.type === 'step.compensated' && event.stepId === 'slow-chain',
    )
  const rootDispatchedAt = log
    .events()
    .findIndex(
      (event) =>
        event.type === 'step.dispatched' &&
        event.stepId === 'root' &&
        event.phase === 'compensate',
    )
  assert.ok(slowConfirmedAt < rootDispatchedAt)
})

test('independent branches are dispatched concurrently within one wave', async () => {
  const inFlight = new Set()
  let maxConcurrent = 0
  const services = {
    svc: {
      async forward({ stepId }) {
        inFlight.add(stepId)
        maxConcurrent = Math.max(maxConcurrent, inFlight.size)
        await new Promise((resolve) => setTimeout(resolve, 15))
        inFlight.delete(stepId)
        return { stepId }
      },
      async compensate({ stepId }) {
        return { stepId }
      },
    },
  }
  const plan = {
    services,
    steps: [
      { id: 'seed', service: 'svc' },
      { id: 'a', service: 'svc', dependsOn: ['seed'] },
      { id: 'b', service: 'svc', dependsOn: ['seed'] },
      { id: 'c', service: 'svc', dependsOn: ['seed'] },
    ],
  }

  const summary = await new Orchestrator({
    plan,
    runId: 'concurrent',
    log: new MemoryEventLog(),
  }).run()

  assert.equal(summary.status, 'completed')
  assert.equal(maxConcurrent, 3, 'a, b and c share a wave')
})

test('a forward-failed step is never compensated', async () => {
  const harness = createServiceHarness()
  const plan = {
    services: harness.services,
    steps: [
      { id: 'root', service: 'svc' },
      { id: 'bad', service: 'svc', dependsOn: ['root'] },
    ],
  }
  harness.script('bad', 'forward', { type: 'fail', message: 'no' })

  const summary = await new Orchestrator({
    plan,
    runId: 'failed-not-compensated',
    log: new MemoryEventLog(),
  }).run()

  assert.equal(harness.callsFor('bad', 'compensate').length, 0)
  assert.equal(harness.callsFor('root', 'compensate').length, 1)
  const bad = summary.steps.find((step) => step.id === 'bad')
  assert.equal(bad.compensation, 'not_needed')
})

test('branch that never started is left pending, not failed and not compensated', async () => {
  const harness = createServiceHarness()
  const plan = {
    services: harness.services,
    steps: [
      { id: 'root', service: 'svc' },
      { id: 'bad', service: 'svc', dependsOn: ['root'] },
      { id: 'after-bad', service: 'svc', dependsOn: ['bad'] },
      { id: 'sibling', service: 'svc', dependsOn: ['root'] },
    ],
  }
  harness.script('bad', 'forward', { type: 'fail', message: 'no', delayMs: 1 })
  harness.script('sibling', 'forward', { type: 'ok', delayMs: 30 })

  const summary = await new Orchestrator({
    plan,
    runId: 'never-started',
    log: new MemoryEventLog(),
  }).run()

  const after = summary.steps.find((step) => step.id === 'after-bad')
  assert.equal(after.forward, 'pending')
  assert.equal(after.compensation, 'not_needed')
  assert.equal(harness.callsFor('after-bad', 'forward').length, 0)

  const sibling = summary.steps.find((step) => step.id === 'sibling')
  assert.equal(sibling.forward, 'succeeded')
  assert.equal(sibling.compensation, 'compensated')
})
