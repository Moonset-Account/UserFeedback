import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createIdempotentExecutionServer } from '../src/server.mjs';
import { createBusiness } from './helpers/business.mjs';
import { createClock } from './helpers/clock.mjs';

const TTL = 60_000;

function makeServer({ business, clock, capacity = 8, snapshot }) {
  let n = 0;
  return createIdempotentExecutionServer({
    adapter: business,
    capacity,
    clock: clock.now,
    executionIdFactory: () => `restart-${++n}`,
    snapshot,
  });
}

function setup(options = {}) {
  const clock = createClock(7_000_000);
  const business = options.business ?? createBusiness();
  const server = makeServer({ business, clock, capacity: options.capacity });
  return { clock, business, server };
}

/** Simulates a process restart: durable snapshot in, fresh server out. */
function restart(server, { business, clock, capacity = 8 }) {
  return makeServer({ business, clock, capacity, snapshot: server.snapshot() });
}

describe('recovery: execution started, effect not yet known', () => {
  it('re-queries the same execution id after a crash started an execution', async () => {
    // The first execute() call is cut off before the business applies anything;
    // the server cannot tell whether it ran, so it records "unknown".
    let executeHits = 0;
    const business = createBusiness();
    const originalExecute = business.execute.bind(business);
    business.execute = async (req) => {
      executeHits += 1;
      if (executeHits === 1) {
        business.executeCalls.push({ executionId: req.executionId, businessKey: req.businessKey });
        return { ok: false, outcome: 'unknown', error: { code: 'PROCESS_DIED' } };
      }
      return originalExecute(req);
    };
    const { clock, server } = setup({ business });

    const first = await server.submit({
      businessKey: 'start-crash',
      params: { task: 'ship' },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(first.status, 'unknown');
    assert.equal(business.effectsFor('start-crash').length, 0);

    // Restart: the business confirms that execution id never took effect.
    const restored = restart(server, { business, clock });
    const result = await restored.recover({ businessKey: 'start-crash' });
    assert.equal(result.status, 'effective');
    assert.equal(result.executionId, first.executionId, 'recovery must continue the same execution id');
    assert.equal(business.executeCalls.at(-1).executionId, first.executionId);
    assert.equal(business.effectsFor('start-crash').length, 1, 'the effect is applied exactly once');
  });

  it('finds an effect that landed even though the response was lost', async () => {
    // Effect applied, then the response dies: the restart must query, not run.
    const business = createBusiness({ afterApply: () => 'unknown' });
    const { clock, server } = setup({ business });
    const first = await server.submit({
      businessKey: 'applied-but-lost',
      params: { task: 'ship' },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(first.status, 'unknown');
    assert.equal(business.effectsFor('applied-but-lost').length, 1);

    const restored = restart(server, { business, clock });
    const result = await restored.recover({ businessKey: 'applied-but-lost' });
    assert.equal(result.status, 'effective');
    assert.equal(result.executionId, first.executionId);
    assert.equal(business.effectsFor('applied-but-lost').length, 1, 'no second application');
    assert.equal(business.executeCalls.length, 1);
  });

  it('only re-runs with the same id when the business says it never took effect', async () => {
    const business = createBusiness();
    let executeHits = 0;
    const originalExecute = business.execute.bind(business);
    business.execute = async (req) => {
      executeHits += 1;
      if (executeHits === 1) {
        // Crash between "execution started" and any business application.
        return { ok: false, outcome: 'unknown', error: { code: 'CRASH' } };
      }
      return originalExecute(req);
    };
    const { clock, server } = setup({ business });
    const first = await server.submit({
      businessKey: 'never-ran',
      params: { n: 1 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(first.status, 'unknown');

    const restored = restart(server, { business, clock });
    const afterRestart = await restored.recover({ businessKey: 'never-ran' });
    assert.equal(afterRestart.status, 'effective');
    assert.equal(afterRestart.executionId, first.executionId, 'the same execution id must continue');
    assert.equal(business.executeCalls.at(-1).executionId, first.executionId);
    assert.equal(business.effectsFor('never-ran').length, 1);
  });
});

describe('recovery: effect applied, receipt lost', () => {
  it('reports "still confirming" while the business outcome is unknown', async () => {
    const business = createBusiness({ queryMode: 'unknown' });
    const originalExecute = business.execute.bind(business);
    business.execute = async (req) => {
      const result = await originalExecute(req);
      return { ok: false, outcome: 'unknown', error: { code: 'RESPONSE_LOST' } };
    };

    const { clock, server } = setup({ business });
    const first = await server.submit({
      businessKey: 'lost-receipt',
      params: { payment: 'p-1' },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(first.status, 'unknown');
    assert.equal(business.effectsFor('lost-receipt').length, 1);

    const restored = restart(server, { business, clock });
    const pending = await restored.recover({ businessKey: 'lost-receipt' });
    assert.equal(pending.status, 'unknown', 'an unknown query must stay unresolved');
    assert.equal(restored.inspect('lost-receipt').outcome, 'unknown');
    assert.equal(business.executeCalls.length, 1, 'recovery must not execute while unconfirmed');

    // The business becomes reachable later; the same id is queried again.
    business.query = async () => ({ status: 'effective', effect: business.receiptFor('lost-receipt') });
    const confirmed = await restored.recover({ businessKey: 'lost-receipt' });
    assert.equal(confirmed.status, 'effective');
    assert.equal(confirmed.executionId, first.executionId);
    assert.equal(business.executeCalls.length, 1);
  });

  it('two clients fetch the same response one after another after restart, effect applied once', async () => {
    // The business applies the effect, then the response (receipt) is lost.
    const business = createBusiness({
      afterApply() {
        return 'unknown'; // response lost on the wire
      },
    });
    const { clock, server } = setup({ business });

    const original = await server.submit({
      businessKey: 'demo-key',
      params: { orderId: 'ord-42', total: 4200 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(original.status, 'unknown');
    assert.equal(business.effectsFor('demo-key').length, 1, 'effect applied exactly once before the crash');

    // Restart; recovery verifies the effect through the stable execution id.
    const restored = restart(server, { business, clock });
    const recovered = await restored.recover({ businessKey: 'demo-key' });
    assert.equal(recovered.status, 'effective');

    // Client one and client two arrive in sequence and each get the response.
    const clientOne = await restored.submit({
      businessKey: 'demo-key',
      params: { orderId: 'ord-42', total: 4200 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    const clientTwo = await restored.submit({
      businessKey: 'demo-key',
      params: { total: 4200, orderId: 'ord-42' }, // same params, different member order
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });

    assert.equal(clientOne.status, 'effective');
    assert.equal(clientTwo.status, 'effective');
    assert.equal(clientTwo.join, 'record');
    assert.equal(clientOne.effect.receiptId, clientTwo.effect.receiptId);
    assert.equal(clientOne.executionId, original.executionId);
    assert.equal(business.effectsFor('demo-key').length, 1, 'business took effect exactly once');
    assert.equal(business.executeCalls.length, 1, 'recovery never re-ran the business');
  });

  it('serves the recorded receipt when a receipt was already stored before the crash', async () => {
    const business = createBusiness();
    const { clock, server } = setup({ business });
    const first = await server.submit({
      businessKey: 'stored',
      params: { v: 1 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(first.status, 'effective');

    const restored = restart(server, { business, clock });
    const recovered = await restored.recover({ businessKey: 'stored' });
    assert.equal(recovered.status, 'effective');
    assert.equal(recovered.executionId, first.executionId);

    const again = await restored.submit({
      businessKey: 'stored',
      params: { v: 1 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(again.status, 'effective');
    assert.equal(again.effect.receiptId, first.effect.receiptId);
    assert.equal(business.executeCalls.length, 1);
  });
});

describe('recovery: refused shortcuts', () => {
  it('never mints a second execution id for a restored key', async () => {
    const business = createBusiness({ afterApply: () => 'unknown' });
    const { clock, server } = setup({ business });
    const first = await server.submit({
      businessKey: 'no-new-id',
      params: { v: 1 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    const restored = restart(server, { business, clock });
    await restored.recover({ businessKey: 'no-new-id' });
    const again = await restored.submit({
      businessKey: 'no-new-id',
      params: { v: 1 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(again.executionId, first.executionId);
    assert.equal(business.executeCalls.length, 1);
  });

  it('treats an adapter exception as unknown, never as "not effective"', async () => {
    const business = createBusiness({ afterApply: () => 'unknown' });
    const originalExecute = business.execute.bind(business);
    let thrown = false;
    business.execute = async (req) => {
      if (!thrown) {
        thrown = true;
        // Consume the execution without letting the server see a result.
        business.executeCalls.push({ executionId: req.executionId, businessKey: req.businessKey });
        throw Object.assign(new Error('socket hang up'), { code: 'ECONNRESET' });
      }
      return originalExecute(req);
    };
    const { clock, server } = setup({ business });
    const first = await server.submit({
      businessKey: 'thrown',
      params: { v: 1 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(first.status, 'unknown');
    const state = server.inspect('thrown');
    assert.equal(state.safeToReuse, false, 'an exception must not license a fresh id');

    const retry = await server.submit({
      businessKey: 'thrown',
      params: { v: 1 },
      retryTtl: TTL,
      deadline: clock.now() + 1_000,
    });
    assert.equal(retry.executionId, first.executionId, 'the retry must continue the same id');
    assert.equal(business.effectsFor('thrown').length, 1);
  });

  it('recovers every resident record at startup', async () => {
    const business = createBusiness({ afterApply: () => 'unknown' });
    const { clock, server } = setup({ business });
    for (const key of ['k1', 'k2', 'k3']) {
      await server.submit({ businessKey: key, params: { key }, retryTtl: TTL, deadline: clock.now() + 1_000 });
    }
    const restored = restart(server, { business, clock });
    business.query = async (req) => ({ status: 'effective', effect: business.receiptFor(req.businessKey) });
    const results = await restored.recoverAll();
    assert.equal(results.size, 3);
    for (const value of results.values()) assert.equal(value.status, 'effective');
    assert.equal(business.effectsFor('k1').length, 1);
    assert.equal(business.effectsFor('k2').length, 1);
    assert.equal(business.effectsFor('k3').length, 1);
  });
});
