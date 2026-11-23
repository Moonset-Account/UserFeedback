import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { setTimeout as delay } from 'node:timers/promises';

import { createIdempotentExecutionServer } from '../src/server.mjs';
import { createBusiness } from './helpers/business.mjs';
import { createClock } from './helpers/clock.mjs';

const TTL = 60_000;

function setup(options = {}) {
  const clock = createClock(5_000_000);
  const business = createBusiness(options.business);
  let n = 0;
  const server = createIdempotentExecutionServer({
    adapter: business,
    capacity: 8,
    clock: clock.now,
    executionIdFactory: () => `concurrent-${++n}`,
  });
  return { clock, business, server };
}

describe('concurrent callers on one key', () => {
  it('runs the business once while every caller gets its own wait window', async () => {
    const { server, business, clock } = setup({ business: { latencyMs: 80 } });
    // Caller A's own wait ends early; caller B waits for the effect.
    const early = server.submit({
      businessKey: 'transfer-9',
      params: { to: 'acct-2', amount: 50 },
      retryTtl: TTL,
      deadline: clock.now() + 10,
    });
    const patient = server.submit({
      businessKey: 'transfer-9',
      params: { to: 'acct-2', amount: 50 },
      retryTtl: TTL,
      deadline: clock.now() + 5_000,
    });

    const [first, second] = await Promise.all([early, patient]);
    assert.equal(first.status, 'pending', 'the short-waiting caller must not be made to wait');
    assert.match(first.join, /started|flight/);
    assert.equal(second.status, 'effective', 'the patient caller must get the receipt');
    assert.equal(business.executeCalls.length, 1);
    assert.equal(business.effectsFor('transfer-9').length, 1);
    assert.equal(first.executionId, second.executionId, 'both callers share one execution id');
    assert.equal(clock.now(), 5_000_000, 'the injected clock never moved for real-time waits');
  });

  it('returns the same receipt to a late caller after the effect landed', async () => {
    const { server, business, clock } = setup({ business: { latencyMs: 20 } });
    const first = await server.submit({
      businessKey: 'late',
      params: { x: 1 },
      retryTtl: TTL,
      deadline: clock.now() + 5_000,
    });
    await delay(5);
    const second = await server.submit({
      businessKey: 'late',
      params: { x: 1 },
      retryTtl: TTL,
      deadline: clock.now() + 5_000,
    });
    assert.equal(second.join, 'record');
    assert.equal(second.effect.receiptId, first.effect.receiptId);
    assert.equal(business.effectsFor('late').length, 1);
  });

  it('an aborted caller stops waiting without cancelling the shared execution', async () => {
    const { server, business, clock } = setup({ business: { latencyMs: 60 } });
    const controller = new AbortController();
    const pending = server.submit({
      businessKey: 'abort',
      params: { job: 'compile' },
      retryTtl: TTL,
      deadline: clock.now() + 10_000,
      signal: controller.signal,
    });
    controller.abort();
    const aborted = await pending;
    assert.equal(aborted.status, 'pending');

    const later = await server.status({ businessKey: 'abort' });
    assert.equal(later.status, 'pending');
    const settled = await server.submit({
      businessKey: 'abort',
      params: { job: 'compile' },
      retryTtl: TTL,
      deadline: clock.now() + 5_000,
    });
    assert.equal(settled.status, 'effective');
    assert.equal(business.executeCalls.length, 1);
  });

  it('many simultaneous callers still produce exactly one business effect', async () => {
    const { server, business, clock } = setup({ business: { latencyMs: 25 } });
    const callers = Array.from({ length: 25 }, () =>
      server.submit({
        businessKey: 'hot-key',
        params: { seq: [1, 2, 3] },
        retryTtl: TTL,
        deadline: clock.now() + 5_000,
      }),
    );
    const results = await Promise.all(callers);
    assert.equal(business.executeCalls.length, 1);
    assert.equal(business.effectsFor('hot-key').length, 1);
    const receiptIds = new Set(results.filter((r) => r.status === 'effective').map((r) => r.effect.receiptId));
    assert.equal(receiptIds.size, 1);
    const ids = new Set(results.map((r) => r.executionId));
    assert.equal(ids.size, 1);
  });

  it('different keys are executed independently', async () => {
    const { server, business, clock } = setup({ business: { latencyMs: 5 } });
    const results = await Promise.all(
      ['a', 'b', 'c'].map((key) =>
        server.submit({
          businessKey: key,
          params: { key },
          retryTtl: TTL,
          deadline: clock.now() + 5_000,
        }),
      ),
    );
    assert.equal(business.executeCalls.length, 3);
    assert.equal(new Set(results.map((r) => r.executionId)).size, 3);
  });
});
