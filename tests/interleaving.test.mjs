import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createIdempotentExecutionServer } from '../src/server.mjs';
import { createBusiness } from './helpers/business.mjs';
import { createClock } from './helpers/clock.mjs';

const TTL = 60_000;

function makeServer({ business, clock, snapshot, capacity = 8 }) {
  let n = 0;
  return createIdempotentExecutionServer({
    adapter: business,
    capacity,
    clock: clock.now,
    executionIdFactory: () => `interleave-${++n}`,
    snapshot,
  });
}

describe('interleaving: crash at each step of the execution lifecycle', () => {
  it('crash after admission, before any execution was dispatched', async () => {
    const clock = createClock(1_000_000);
    const business = createBusiness();
    // Hand-crafted crash image: the record was admitted, no execution id yet.
    const snapshot = {
      records: [
        {
          businessKey: 'admitted-only',
          parameters: { v: 1 },
          acceptedAt: clock.now(),
          retryDeadline: clock.now() + TTL,
          executionId: null,
          delivered: false,
        },
      ],
    };
    const restored = makeServer({ business, clock, snapshot });
    assert.equal(restored.status({ businessKey: 'admitted-only' }).status, 'pending');

    const recovered = await restored.recover({ businessKey: 'admitted-only' });
    assert.equal(recovered.status, 'effective');
    assert.deepEqual(recovered.checks, ['no-execution-recorded', 'effective']);
    assert.equal(business.effectsFor('admitted-only').length, 1);
  });

  it('crash after the execution id was recorded, before the business applied', async () => {
    const clock = createClock(2_000_000);
    const business = createBusiness();
    const snapshot = {
      records: [
        {
          businessKey: 'id-recorded',
          parameters: { v: 2 },
          acceptedAt: clock.now(),
          retryDeadline: clock.now() + TTL,
          executionId: 'stable-id-1',
          delivered: false,
        },
      ],
    };
    const restored = makeServer({ business, clock, snapshot });
    assert.equal(restored.status({ businessKey: 'id-recorded' }).status, 'unknown');

    const recovered = await restored.recover({ businessKey: 'id-recorded' });
    assert.equal(recovered.status, 'effective');
    assert.equal(recovered.executionId, 'stable-id-1', 'must continue the recorded id');
    assert.deepEqual(business.executeCalls.map((c) => c.executionId), ['stable-id-1']);
    assert.equal(business.effectsFor('id-recorded').length, 1);
  });

  it('crash after the business applied, before the response was recorded', async () => {
    const clock = createClock(3_000_000);
    const business = createBusiness({ afterApply: () => 'unknown' });
    const server = makeServer({ business, clock });
    const original = await server.submit({
      businessKey: 'applied-before-record',
      params: { v: 3 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(original.status, 'unknown');
    assert.equal(business.effectsFor('applied-before-record').length, 1);

    const restored = makeServer({ business, clock, snapshot: server.snapshot() });
    const recovered = await restored.recover({ businessKey: 'applied-before-record' });
    assert.equal(recovered.status, 'effective');
    assert.equal(recovered.executionId, original.executionId);
    assert.equal(business.effectsFor('applied-before-record').length, 1);
    assert.equal(business.executeCalls.length, 1);
  });

  it('crash after the response was recorded, before the caller received it', async () => {
    const clock = createClock(4_000_000);
    const business = createBusiness();
    const server = makeServer({ business, clock });
    const original = await server.submit({
      businessKey: 'recorded-not-delivered',
      params: { v: 4 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(original.status, 'effective');

    // The caller never saw it; a restart must still serve the same receipt
    // without asking the business to run again.
    const restored = makeServer({ business, clock, snapshot: server.snapshot() });
    const recovered = await restored.recover({ businessKey: 'recorded-not-delivered' });
    assert.equal(recovered.status, 'effective');
    const resent = await restored.submit({
      businessKey: 'recorded-not-delivered',
      params: { v: 4 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(resent.effect.receiptId, original.effect.receiptId);
    assert.equal(business.executeCalls.length, 1);
  });
});

describe('interleaving: callers arriving at different recovery moments', () => {
  it('callers arriving during an unknown verification do not start their own execution', async () => {
    const clock = createClock(5_000_000);
    let releaseQuery;
    const gate = new Promise((resolve) => {
      releaseQuery = resolve;
    });
    const business = createBusiness({ afterApply: () => 'unknown' });
    const server = makeServer({ business, clock });
    const original = await server.submit({
      businessKey: 'gate',
      params: { v: 5 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(original.status, 'unknown');

    // Restart and make the verification query hang until we release it.
    const restored = makeServer({ business, clock, snapshot: server.snapshot() });
    business.query = async () => {
      await gate;
      return { status: 'effective', effect: business.receiptFor('gate') };
    };

    const recovery = restored.recover({ businessKey: 'gate' });
    const earlyCaller = restored.submit({
      businessKey: 'gate',
      params: { v: 5 },
      retryTtl: TTL,
      deadline: clock.now() + 5_000,
    });
    releaseQuery();

    const [recovered, early] = await Promise.all([recovery, earlyCaller]);
    assert.equal(recovered.status, 'effective');
    assert.equal(early.status, 'effective');
    assert.equal(early.executionId, original.executionId);
    assert.equal(business.executeCalls.length, 1);
    assert.equal(business.effectsFor('gate').length, 1);
  });

  it('a caller that times out and retries later reuses the finished execution', async () => {
    const clock = createClock(6_000_000);
    const business = createBusiness({ latencyMs: 30 });
    const server = makeServer({ business, clock });

    const timedOut = await server.submit({
      businessKey: 'late-retry',
      params: { v: 6 },
      retryTtl: TTL,
      deadline: clock.now() + 5,
    });
    assert.equal(timedOut.status, 'pending');
    const id = timedOut.executionId;

    const retried = await server.submit({
      businessKey: 'late-retry',
      params: { v: 6 },
      retryTtl: TTL,
      deadline: clock.now() + 5_000,
    });
    assert.equal(retried.status, 'effective');
    assert.equal(retried.executionId, id);
    assert.equal(business.executeCalls.length, 1);
    assert.equal(business.effectsFor('late-retry').length, 1);
  });

  it('a caller with different params during recovery gets a conflict, not a second run', async () => {
    const clock = createClock(7_000_000);
    const business = createBusiness({ afterApply: () => 'unknown' });
    const server = makeServer({ business, clock });
    const original = await server.submit({
      businessKey: 'conflict-during-recovery',
      params: { v: 7 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });

    const restored = makeServer({ business, clock, snapshot: server.snapshot() });
    const clash = await restored.submit({
      businessKey: 'conflict-during-recovery',
      params: { v: 8 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(clash.accepted, false);
    assert.equal(clash.reason, 'PARAMETER_MISMATCH');
    assert.equal(business.effectsFor('conflict-during-recovery').length, 1);
    assert.equal(business.executeCalls.length, 1);
    assert.equal(restored.inspect('conflict-during-recovery').executionId, original.executionId);
  });
});
