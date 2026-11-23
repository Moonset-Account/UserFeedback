import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createIdempotentExecutionServer } from '../src/server.mjs';
import { createBusiness } from './helpers/business.mjs';
import { createClock } from './helpers/clock.mjs';

const TTL = 10_000;

function setup(options = {}) {
  const clock = createClock(100_000);
  const business = options.business ?? createBusiness();
  let n = 0;
  const server = createIdempotentExecutionServer({
    adapter: business,
    capacity: options.capacity ?? 4,
    clock: clock.now,
    executionIdFactory: () => `bound-${++n}`,
  });
  return { clock, business, server };
}

describe('parameter binding inside the retry window', () => {
  it('reports a conflict for different parameters under a live key', async () => {
    const { server, clock, business } = setup();
    const first = await server.submit({
      businessKey: 'pay-1',
      params: { amount: 100 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(first.status, 'effective');

    const clash = await server.submit({
      businessKey: 'pay-1',
      params: { amount: 999 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(clash.accepted, false);
    assert.equal(clash.reason, 'PARAMETER_MISMATCH');
    assert.equal(clash.retryDeadline, first.retryDeadline);
    assert.equal(clash.retryInMs, TTL);
    assert.equal(business.effectsFor('pay-1').length, 1, 'the conflicting call must not execute');
  });

  it('keeps the conflict for the whole retry window and releases the key after it', async () => {
    const { server, clock, business } = setup();
    await server.submit({
      businessKey: 'slot',
      params: { v: 1 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });

    clock.advance(TTL - 1);
    const lastMoment = await server.submit({
      businessKey: 'slot',
      params: { v: 2 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(lastMoment.reason, 'PARAMETER_MISMATCH');

    clock.advance(1); // now === retryDeadline: the binding has lapsed
    const afterWindow = await server.submit({
      businessKey: 'slot',
      params: { v: 2 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(afterWindow.accepted, true);
    assert.equal(afterWindow.status, 'effective');
    assert.deepEqual(afterWindow.effect.operation, { v: 2 });
    assert.equal(business.effectsFor('slot').length, 2, 'a rebound key is a new business operation');
  });

  it('a conflicting caller cannot extend or shorten the original window', async () => {
    const { server, clock } = setup();
    const first = await server.submit({
      businessKey: 'window',
      params: { a: 1 },
      retryTtl: TTL,
      deadline: clock.now() + 500,
    });
    await server.submit({
      businessKey: 'window',
      params: { a: 2 },
      retryTtl: TTL * 100,
      deadline: clock.now() + 500,
    });
    clock.advance(TTL + 1);
    const rebound = await server.submit({
      businessKey: 'window',
      params: { a: 3 },
      retryTtl: TTL,
      deadline: clock.now() + 500,
    });
    assert.equal(rebound.accepted, true);
    assert.equal(rebound.retryDeadline, clock.now() + TTL);
    assert.ok(rebound.retryDeadline > first.retryDeadline);
  });

  it('does not rebind a key whose previous effect is still unknown', async () => {
    const business = createBusiness({ queryMode: 'unknown' });
    const { server, clock } = setup({ business });
    // The execute path reports unknown, so the key stays unresolved.
    business.execute = async (req) => {
      business.executeCalls.push(req);
      return { ok: false, outcome: 'unknown', error: { code: 'UNREACHABLE' } };
    };
    const first = await server.submit({
      businessKey: 'unknown-slot',
      params: { v: 1 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(first.status, 'unknown');

    clock.advance(TTL + 1);
    const rebind = await server.submit({
      businessKey: 'unknown-slot',
      params: { v: 2 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(rebind.accepted, false);
    assert.equal(rebind.reason, 'PENDING_CONFIRMATION');
  });
});

describe('bounded idempotency window', () => {
  it('rejects a new key when the cache is full and every record is inside its window', async () => {
    const { server, clock } = setup({ capacity: 2 });
    for (const key of ['a', 'b']) {
      const res = await server.submit({
        businessKey: key,
        params: { key },
        retryTtl: TTL,
        deadline: clock.now() + 1_000,
      });
      assert.equal(res.accepted, true);
    }
    assert.equal(server.size, 2);

    const rejected = await server.submit({
      businessKey: 'c',
      params: { key: 'c' },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(rejected.accepted, false);
    assert.equal(rejected.reason, 'CAPACITY');
    assert.equal(rejected.capacity, 2);
    assert.equal(server.size, 2);

    // Existing keys are still served at capacity.
    const hit = await server.submit({
      businessKey: 'a',
      params: { key: 'a' },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(hit.accepted, true);
    assert.equal(hit.status, 'effective');
  });

  it('admits a new key by evicting the oldest record once it is outside its retry window', async () => {
    const { server, clock, business } = setup({ capacity: 2 });
    await server.submit({ businessKey: 'old', params: { k: 'old' }, retryTtl: 1_000, deadline: clock.now() + 100 });
    await server.submit({ businessKey: 'new', params: { k: 'new' }, retryTtl: TTL, deadline: clock.now() + 100 });

    clock.advance(1_000); // 'old' expires, 'new' does not
    const admitted = await server.submit({
      businessKey: 'third',
      params: { k: 'third' },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(admitted.accepted, true);
    assert.equal(admitted.evicted, 'old');
    assert.equal(server.size, 2);
    assert.equal(server.status({ businessKey: 'new' }).status, 'effective');
    assert.equal(business.effectsFor('new').length, 1);
  });

  it('does not evict an unresolved record even after its retry window', async () => {
    const business = createBusiness();
    business.execute = async (req) => {
      business.executeCalls.push(req);
      return { ok: false, outcome: 'unknown', error: { code: 'UNREACHABLE' } };
    };
    const { server, clock } = setup({ capacity: 1, business });
    const first = await server.submit({
      businessKey: 'unresolved',
      params: { k: 1 },
      retryTtl: 1_000,
      deadline: clock.now() + 100,
    });
    assert.equal(first.status, 'unknown');

    clock.advance(10_000);
    const blocked = await server.submit({
      businessKey: 'fresh',
      params: { k: 2 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(blocked.reason, 'CAPACITY', 'dropping an unknown effect would risk a double application');
    assert.equal(server.inspect('unresolved').executionId, first.executionId);
  });

  it('does not evict a record that is still executing', async () => {
    const business = createBusiness({ latencyMs: 40 });
    const { server, clock } = setup({ capacity: 1, business });
    const running = server.submit({
      businessKey: 'running',
      params: { k: 1 },
      retryTtl: 1_000,
      deadline: clock.now() + 100,
    });
    clock.advance(2_000); // both records are now outside their windows
    const blocked = await server.submit({
      businessKey: 'other',
      params: { k: 2 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(blocked.reason, 'CAPACITY', 'a live execution may not be dropped');
    await running;
  });
});
