import test from 'node:test'
import assert from 'node:assert/strict'

import { MemoryEventLog } from '../src/event-log.mjs'

test('the log assigns monotonic sequence numbers starting at 1', () => {
  const log = new MemoryEventLog()
  const a = log.append({ type: 'run.started', runId: 'r' })
  const b = log.append({ type: 'step.dispatched', stepId: 's', phase: 'forward' })
  const c = log.append({ type: 'run.finished', status: 'completed' })

  assert.deepEqual([a.seq, b.seq, c.seq], [1, 2, 3])
  assert.deepEqual(
    log.events().map((event) => event.seq),
    [1, 2, 3],
  )
})

test('caller-supplied seq and ts are ignored so replay is deterministic', () => {
  const log = new MemoryEventLog()
  const stored = log.append({ type: 'run.started', seq: 99, ts: 1234567890, runId: 'r' })
  assert.equal(stored.seq, 1)
  assert.equal('ts' in stored, false)
})

test('a snapshot round-trips through structuredClone and JSON', () => {
  const log = new MemoryEventLog()
  log.append({ type: 'run.started', runId: 'r' })
  log.append({ type: 'step.succeeded', stepId: 's', commandId: 'op_x', result: { ok: true } })

  const snapshot = log.snapshot()
  const restored = MemoryEventLog.from(JSON.parse(JSON.stringify(snapshot)))

  assert.deepEqual(restored.snapshot(), snapshot)
  assert.deepEqual(
    restored.events().map((event) => event.seq),
    [1, 2],
  )
})

test('appending after restoring a snapshot continues the sequence', () => {
  const log = new MemoryEventLog()
  log.append({ type: 'run.started', runId: 'r' })

  const restored = MemoryEventLog.from(log.snapshot())
  const next = restored.append({ type: 'step.dispatched', stepId: 's', phase: 'forward' })
  assert.equal(next.seq, 2)
})

test('stored events are frozen and malformed events are rejected', () => {
  const log = new MemoryEventLog()
  const stored = log.append({ type: 'run.started', runId: 'r' })
  assert.ok(Object.isFrozen(stored))

  assert.throws(() => log.append({}), /event.type/)
  assert.throws(() => log.append(null), /event.type/)
})
