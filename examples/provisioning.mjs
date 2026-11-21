/**
 * Runnable example: a resource-provisioning DAG where one prerequisite fans
 * out into two branches, the left branch fails, and the right branch reports
 * success late.
 *
 *   network-root ──┬── dns-record
 *   (shared)       └── compute-node ── database
 *
 * Expectation: database is never started, compute-node is undone, and
 * network-root is only undone after compute-node's compensation is confirmed.
 *
 * Run with:  node examples/provisioning.mjs
 */

import { Orchestrator } from '../src/orchestrator.mjs'
import { MemoryEventLog } from '../src/event-log.mjs'
import { createServiceHarness } from '../src/testing.mjs'
import { setTimeout as sleep } from 'node:timers/promises'

const harness = createServiceHarness()

const plan = {
  services: harness.services,
  steps: [
    { id: 'network-root', service: 'network' },
    { id: 'dns-record', service: 'dns', dependsOn: ['network-root'] },
    { id: 'compute-node', service: 'compute', dependsOn: ['network-root'] },
    { id: 'database', service: 'database', dependsOn: ['compute-node'] },
  ],
}

harness.script('dns-record', 'forward', {
  type: 'fail',
  message: 'quota exhausted for dns-record',
  delayMs: 5,
})
harness.script('compute-node', 'forward', { type: 'ok', delayMs: 30 })
harness.script('compute-node', 'compensate', {
  type: 'fail',
  message: 'transient',
}, 'ok')

const log = new MemoryEventLog()
const orchestrator = new Orchestrator({
  plan,
  runId: 'provision-demo',
  log,
  maxCompensationAttempts: 3,
})

const summary = await orchestrator.run()
await sleep(0)

console.log('── run summary ───────────────────────────────────────────')
console.log(JSON.stringify(summary, null, 2))

console.log('\n── recorded events ──────────────────────────────────────')
for (const event of log.events()) {
  const detail =
    event.stepId !== undefined
      ? `${event.stepId}${event.phase ? `:${event.phase}` : ''}`
      : event.status ?? ''
  console.log(
    `#${String(event.seq).padStart(2, '0')} ${event.type.padEnd(24)} ${detail}`,
  )
}

console.log('\n── service calls (credential prefix + attempt) ──────────')
for (const call of harness.calls) {
  console.log(
    `${String(call.order).padStart(2)} ${call.stepId.padEnd(13)} ${call.phase.padEnd(10)} ` +
      `attempt=${call.attempt} cred=${call.credential.slice(0, 12)}… ` +
      `deduped=${call.deduped} outcome=${call.outcome}`,
  )
}

console.log('\n── invariants ───────────────────────────────────────────')
const networkCalls = harness.callsFor('network-root', 'compensate')
const computeCalls = harness.callsFor('compute-node', 'compensate')
console.log(
  `database forward calls:            ${harness.callsFor('database', 'forward').length}`,
)
console.log(
  `compute-node compensation applied: ${harness.appliedCompensationCount('compute-node')}`,
)
console.log(
  `network-root compensation applied: ${harness.appliedCompensationCount('network-root')}`,
)
console.log(
  `compute-node credential stable:    ${new Set(computeCalls.map((c) => c.credential)).size === 1}`,
)
console.log(
  `network-root dispatched last:      ${networkCalls.at(-1)?.order === harness.calls.at(-1)?.order}`,
)
