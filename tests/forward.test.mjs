import test from 'node:test'
import assert from 'node:assert/strict'

import { Orchestrator } from '../src/orchestrator.mjs'
import { MemoryEventLog } from '../src/event-log.mjs'
import { createServiceHarness, diamondPlan } from '../src/testing.mjs'

test('happy path: a diamond runs prerequisites first, join last, nothing compensated', async () => {
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)
  const log = new MemoryEventLog()

  const orchestrator = new Orchestrator({ plan, runId: 'happy', log })
  const summary = await orchestrator.run()

  assert.equal(summary.status, 'completed')
  assert.equal(summary.aborted, false)
  assert.deepEqual(summary.residualEffects, [])

  for (const step of summary.steps) {
    assert.equal(step.forward, 'succeeded')
    assert.equal(step.compensation, 'not_needed')
  }

  const forwardIds = harness.calls
    .filter((call) => call.phase === 'forward')
    .map((call) => call.stepId)
  assert.equal(forwardIds.length, 4)
  assert.equal(forwardIds[0], 'root')
  assert.equal(forwardIds.at(-1), 'join')
  // left and right are dispatched in the same wave and both precede join.
  assert.ok(forwardIds.indexOf('left') < forwardIds.indexOf('join'))
  assert.ok(forwardIds.indexOf('right') < forwardIds.indexOf('join'))

  assert.equal(harness.calls.filter((call) => call.phase === 'compensate').length, 0)
  assert.equal(log.events().at(-1).type, 'run.finished')
})

test('a step is not dispatched until every direct prerequisite succeeded', async () => {
  const harness = createServiceHarness()
  const plan = {
    services: harness.services,
    steps: [
      { id: 'a', service: 'svc', data: { n: 1 } },
      { id: 'b', service: 'svc', data: { n: 2 } },
      { id: 'join', service: 'svc', dependsOn: ['a', 'b'], data: { n: 3 } },
    ],
  }
  harness.script('join', 'forward', 'ok')

  const summary = await new Orchestrator({ plan, runId: 'gate', log: new MemoryEventLog() }).run()
  assert.equal(summary.status, 'completed')

  const dispatched = harness.calls
    .filter((call) => call.phase === 'forward')
    .map((call) => call.stepId)
  assert.deepEqual(dispatched.slice(0, 2), ['a', 'b'])
  assert.equal(dispatched[2], 'join')
})

test('a failing left branch stops new steps; the successful prerequisite is undone once', async () => {
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)
  harness.script('left', 'forward', 'fail')

  const summary = await new Orchestrator({
    plan,
    runId: 'left-fails',
    log: new MemoryEventLog(),
  }).run()

  assert.equal(summary.status, 'compensated')
  assert.equal(summary.aborted, true)
  assert.equal(summary.failedStepId, 'left')

  const byId = Object.fromEntries(summary.steps.map((step) => [step.id, step]))
  assert.equal(byId.root.forward, 'succeeded')
  assert.equal(byId.root.compensation, 'compensated')
  assert.equal(byId.left.forward, 'failed')
  assert.equal(byId.left.compensation, 'not_needed')
  assert.equal(byId.right.forward, 'succeeded')
  assert.equal(byId.right.compensation, 'compensated')

  // `join` was never started because its prerequisites were not all successful.
  assert.equal(byId.join.forward, 'pending')
  assert.equal(harness.callsFor('join', 'forward').length, 0)

  assert.equal(harness.appliedCompensationCount('root'), 1)
  assert.equal(harness.appliedCompensationCount('right'), 1)
})

test('no forward step is started after the first failure, even if it was ready', async () => {
  const harness = createServiceHarness()
  const plan = {
    services: harness.services,
    steps: [
      { id: 'root', service: 'svc' },
      { id: 'left', service: 'svc', dependsOn: ['root'] },
      { id: 'right', service: 'svc', dependsOn: ['root'] },
      { id: 'left-child', service: 'svc', dependsOn: ['left'] },
    ],
  }
  harness.script('left', 'forward', 'fail')

  const summary = await new Orchestrator({
    plan,
    runId: 'stop-start',
    log: new MemoryEventLog(),
  }).run()

  assert.equal(summary.failedStepId, 'left')
  assert.equal(harness.callsFor('left-child', 'forward').length, 0)
  // right was in the same wave as the failure and was allowed to finish.
  assert.equal(harness.callsFor('right', 'forward').length, 1)
  assert.equal(harness.appliedCompensationCount('right'), 1)
  assert.equal(harness.appliedCompensationCount('root'), 1)
})
