import test from 'node:test'
import assert from 'node:assert/strict'

import { Orchestrator } from '../src/orchestrator.mjs'
import { MemoryEventLog } from '../src/event-log.mjs'
import { createServiceHarness, diamondPlan } from '../src/testing.mjs'

test('compensation runs in reverse dependency order: join, branches, then shared root', async () => {
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)
  harness.script('join', 'forward', 'fail')

  const summary = await new Orchestrator({
    plan,
    runId: 'reverse-order',
    log: new MemoryEventLog(),
  }).run()

  assert.equal(summary.status, 'compensated')

  const compensated = harness.calls
    .filter((call) => call.phase === 'compensate')
    .map((call) => call.stepId)
  assert.deepEqual(new Set(compensated), new Set(['root', 'left', 'right']))
  assert.equal(compensated.at(-1), 'root', 'shared prerequisite must be compensated last')
  assert.ok(compensated.indexOf('left') < compensated.indexOf('root'))
  assert.ok(compensated.indexOf('right') < compensated.indexOf('root'))
})

test('one compensation per successful step even with a diamond', async () => {
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)
  harness.script('right', 'forward', 'fail')

  await new Orchestrator({ plan, runId: 'once', log: new MemoryEventLog() }).run()

  for (const stepId of ['root', 'left']) {
    assert.equal(harness.appliedCompensationCount(stepId), 1, `${stepId} undone once`)
  }
  assert.equal(harness.callsFor('right', 'compensate').length, 0)
  assert.equal(harness.callsFor('join', 'compensate').length, 0)
})

test('compensation retries reuse the same operation credential and succeed once', async () => {
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)
  harness.script('join', 'forward', 'fail')
  // root fails compensation twice, then succeeds.
  harness.script('root', 'compensate', 'fail', 'fail', 'ok')

  const summary = await new Orchestrator({
    plan,
    runId: 'retry',
    log: new MemoryEventLog(),
    maxCompensationAttempts: 5,
  }).run()

  assert.equal(summary.status, 'compensated')
  const attempts = harness.callsFor('root', 'compensate')
  assert.equal(attempts.length, 3)
  assert.deepEqual(attempts.map((call) => call.attempt), [1, 2, 3])
  assert.equal(new Set(attempts.map((call) => call.credential)).size, 1)
  assert.equal(harness.appliedCompensationCount('root'), 1)
})

test('a prerequisite is not undone while a successful successor lacks compensation', async () => {
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)
  harness.script('join', 'forward', 'fail')
  // left cannot be undone; this must hold up root indefinitely.
  harness.script('left', 'compensate', { type: 'fail', message: 'left stuck' })

  const summary = await new Orchestrator({
    plan,
    runId: 'blocked',
    log: new MemoryEventLog(),
    maxCompensationAttempts: 2,
  }).run()

  assert.equal(summary.status, 'compensation_incomplete')
  assert.equal(harness.callsFor('root', 'compensate').length, 0)
  assert.equal(harness.callsFor('left', 'compensate').length, 2)
  assert.equal(harness.callsFor('right', 'compensate').length, 1)

  const root = summary.steps.find((step) => step.id === 'root')
  assert.equal(root.compensation, 'abandoned')
  assert.deepEqual(summary.residualEffects.map((effect) => effect.stepId).sort(), [
    'left',
    'root',
  ])
})

test('a blocked successor propagates the block transitively up the chain', async () => {
  const harness = createServiceHarness()
  const plan = {
    services: harness.services,
    steps: [
      { id: 'root', service: 'svc' },
      { id: 'mid', service: 'svc', dependsOn: ['root'] },
      { id: 'deep', service: 'svc', dependsOn: ['mid'] },
      { id: 'boom', service: 'svc', dependsOn: ['mid'] },
    ],
  }
  // deep and boom share the third wave: deep succeeds, boom fails.  deep then
  // cannot be undone, which must block mid and transitively root.
  harness.script('boom', 'forward', { type: 'fail', message: 'boom', delayMs: 1 })
  harness.script('deep', 'forward', { type: 'ok', delayMs: 15 })
  harness.script('deep', 'compensate', { type: 'fail', message: 'deep stuck' })

  const log = new MemoryEventLog()
  const summary = await new Orchestrator({
    plan,
    runId: 'transitive-block',
    log,
    maxCompensationAttempts: 2,
  }).run()

  // deep exhausts retries; mid and root must never be undone out of order.
  assert.equal(summary.steps.find((step) => step.id === 'deep').compensation, 'abandoned')
  assert.equal(summary.steps.find((step) => step.id === 'mid').compensation, 'abandoned')
  assert.equal(summary.steps.find((step) => step.id === 'root').compensation, 'abandoned')
  assert.equal(harness.callsFor('mid', 'compensate').length, 0)
  assert.equal(harness.callsFor('root', 'compensate').length, 0)

  const blocked = log
    .events()
    .filter((event) => event.type === 'step.abandoned' && event.reason === 'blocked_by_successor')
    .map((event) => [event.stepId, event.blockedBy])
  assert.deepEqual(blocked, [
    ['mid', ['deep']],
    ['root', ['mid']],
  ])
})

test('compensation failure is retried up to the configured limit then abandoned', async () => {
  const harness = createServiceHarness()
  const plan = diamondPlan(harness.services)
  harness.script('join', 'forward', 'fail')
  harness.script('right', 'compensate', { type: 'fail', message: 'always' })

  const log = new MemoryEventLog()
  const summary = await new Orchestrator({
    plan,
    runId: 'exhaust',
    log,
    maxCompensationAttempts: 3,
  }).run()

  const rightCalls = harness.callsFor('right', 'compensate')
  assert.equal(rightCalls.length, 3)
  assert.equal(new Set(rightCalls.map((call) => call.credential)).size, 1)

  const right = summary.steps.find((step) => step.id === 'right')
  assert.equal(right.compensation, 'abandoned')

  const abandoned = log
    .events()
    .filter((event) => event.type === 'step.abandoned')
    .map((event) => [event.stepId, event.reason])
  assert.deepEqual(abandoned, [
    ['right', 'compensation_failed'],
    ['root', 'blocked_by_successor'],
  ])

  assert.deepEqual(
    summary.residualEffects.map((effect) => [effect.stepId, effect.state]),
    [
      ['root', 'abandoned'],
      ['right', 'abandoned'],
    ],
  )
})
