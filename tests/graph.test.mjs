import test from 'node:test'
import assert from 'node:assert/strict'

import { validatePlan, PlanError, topologicalOrder, ancestorsOf } from '../src/graph.mjs'
import { credentialFor } from '../src/credentials.mjs'

const stubServices = {
  a: { forward: async () => ({}), compensate: async () => ({}) },
  b: { forward: async () => ({}), compensate: async () => ({}) },
}

test('validates a well-formed plan and returns a deterministic order', () => {
  const plan = {
    services: stubServices,
    steps: [
      { id: 'join', service: 'a', dependsOn: ['left', 'right'] },
      { id: 'left', service: 'a', dependsOn: ['root'] },
      { id: 'root', service: 'b' },
      { id: 'right', service: 'a', dependsOn: ['root'] },
    ],
  }
  const { order, ids, dependents, prerequisites } = validatePlan(plan)
  assert.deepEqual(new Set(order), new Set(ids))
  assert.equal(order[0], 'root')
  assert.equal(order.at(-1), 'join')
  assert.deepEqual(prerequisites.get('join'), ['left', 'right'])
  assert.deepEqual(new Set(dependents.get('root')), new Set(['left', 'right']))
  assert.deepEqual([...ancestorsOf('join', prerequisites)].sort(), ['left', 'right', 'root'])
})

test('rejects a dependency cycle', () => {
  const plan = {
    services: stubServices,
    steps: [
      { id: 'a', service: 'a', dependsOn: ['b'] },
      { id: 'b', service: 'a', dependsOn: ['a'] },
    ],
  }
  assert.throws(() => validatePlan(plan), (error) => {
    assert.ok(error instanceof PlanError)
    assert.match(error.message, /cycle/)
    return true
  })
})

test('rejects duplicate ids, unknown prerequisites, self-dependency and empty plans', () => {
  assert.throws(
    () =>
      validatePlan({
        services: stubServices,
        steps: [
          { id: 'a', service: 'a' },
          { id: 'a', service: 'b' },
        ],
      }),
    /duplicate step id/,
  )

  assert.throws(
    () => validatePlan({ services: stubServices, steps: [{ id: 'a', service: 'a', dependsOn: ['ghost'] }] }),
    /unknown prerequisite/,
  )

  assert.throws(
    () => validatePlan({ services: stubServices, steps: [{ id: 'a', service: 'a', dependsOn: ['a'] }] }),
    /cannot depend on itself/,
  )

  assert.throws(() => validatePlan({ services: stubServices, steps: [] }), /non-empty array/)

  assert.throws(
    () => validatePlan({ services: {}, steps: [{ id: 'a', service: 'missing' }] }),
    /no forward\(\) implementation/,
  )

  assert.throws(
    () =>
      validatePlan({
        services: { a: { forward: async () => ({}) } },
        steps: [{ id: 'a', service: 'a' }],
      }),
    /no compensate\(\) implementation/,
  )
})

test('topologicalOrder detects a cycle even when called directly', () => {
  const ids = ['a', 'b', 'c']
  const prerequisites = new Map([
    ['a', ['c']],
    ['b', ['a']],
    ['c', ['b']],
  ])
  const dependents = new Map([
    ['a', ['b']],
    ['b', ['c']],
    ['c', ['a']],
  ])
  assert.throws(() => topologicalOrder(ids, prerequisites, dependents), /cycle/)
})

test('operation credentials are stable, phase-specific and run-specific', () => {
  const first = credentialFor('run-1', 'step-a', 'forward')
  const again = credentialFor('run-1', 'step-a', 'forward')
  const otherPhase = credentialFor('run-1', 'step-a', 'compensate')
  const otherRun = credentialFor('run-2', 'step-a', 'forward')

  assert.equal(first, again)
  assert.notEqual(first, otherPhase)
  assert.notEqual(first, otherRun)
  assert.match(first, /^op_[0-9a-f]{32}$/)
})
