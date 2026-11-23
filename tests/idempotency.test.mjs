import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createIdempotentExecutionServer, parameterIdentity } from '../src/server.mjs';
import { createBusiness } from './helpers/business.mjs';
import { createClock } from './helpers/clock.mjs';

const TTL = 60_000;

function setup(options = {}) {
  const clock = options.clock ?? createClock();
  const business = options.business ?? createBusiness();
  let n = 0;
  const server = createIdempotentExecutionServer({
    adapter: business,
    capacity: options.capacity ?? 8,
    clock: clock.now,
    executionIdFactory: () => `exec-${++n}`,
    ...options.serverOptions,
  });
  const at = () => clock.now();
  return { clock, business, server, at };
}

describe('basic idempotent execution', () => {
  it('applies the business effect once and returns the receipt', async () => {
    const { server, business, at } = setup();
    const result = await server.submit({
      businessKey: 'order-1',
      params: { amount: 10, currency: 'CNY' },
      retryTtl: TTL,
      deadline: at() + 1_000,
    });

    assert.equal(result.accepted, true);
    assert.equal(result.status, 'effective');
    assert.deepEqual(result.effect.operation, { amount: 10, currency: 'CNY' });
    assert.equal(business.effectsFor('order-1').length, 1);
    assert.equal(business.executeCalls.length, 1);
  });

  it('same key and same params share one execution and one receipt', async () => {
    const { server, business, at } = setup();
    const params = { id: 7, tags: ['a', 'b'] };
    const [first, second] = await Promise.all([
      server.submit({ businessKey: 'shared', params, retryTtl: TTL, deadline: at() + 1_000 }),
      server.submit({ businessKey: 'shared', params, retryTtl: TTL, deadline: at() + 1_000 }),
    ]);

    assert.equal(business.executeCalls.length, 1);
    assert.equal(business.effectsFor('shared').length, 1);
    assert.equal(first.effect.receiptId, second.effect.receiptId);
    assert.deepEqual(
      [first.join, second.join].sort(),
      ['flight', 'started'],
    );
  });

  it('treats parameter objects as equal regardless of member order', async () => {
    const { server, business, at } = setup();
    const one = await server.submit({
      businessKey: 'o',
      params: { a: 1, b: 2 },
      retryTtl: TTL,
      deadline: at() + 1_000,
    });
    const two = await server.submit({
      businessKey: 'o',
      params: { b: 2, a: 1 },
      retryTtl: TTL,
      deadline: at() + 1_000,
    });

    assert.equal(two.accepted, true);
    assert.equal(two.join, 'record');
    assert.equal(two.effect.receiptId, one.effect.receiptId);
    assert.equal(business.executeCalls.length, 1);
    assert.equal(parameterIdentity({ a: 1, b: 2 }), parameterIdentity({ b: 2, a: 1 }));
  });

  it('separates values that only look alike after JSON coercion', () => {
    assert.notEqual(parameterIdentity({ v: 1 }), parameterIdentity({ v: '1' }));
    assert.notEqual(parameterIdentity([1, 2]), parameterIdentity([2, 1]));
    assert.notEqual(parameterIdentity(0), parameterIdentity(-0));
    assert.notEqual(parameterIdentity({ a: null }), parameterIdentity({}));
    assert.equal(parameterIdentity({ nested: { z: 1, y: [true, null] } }), parameterIdentity({ nested: { y: [true, null], z: 1 } }));
  });

  it('rejects parameters that have no stable identity', async () => {
    const { server, at } = setup();
    for (const params of [undefined, { f: () => 1 }, { n: Number.NaN }, { d: new Date() }, { b: 1n }]) {
      const result = await server.submit({
        businessKey: 'bad',
        params,
        retryTtl: TTL,
        deadline: at() + 100,
      });
      assert.equal(result.accepted, false);
      assert.equal(result.reason, 'INVALID_PARAMS');
    }
    const cyclic = {};
    cyclic.self = cyclic;
    const result = await server.submit({
      businessKey: 'bad',
      params: cyclic,
      retryTtl: TTL,
      deadline: at() + 100,
    });
    assert.equal(result.reason, 'INVALID_PARAMS');
  });

  it('rejects missing or malformed admission inputs', async () => {
    const { server, at } = setup();
    const base = { params: {}, retryTtl: TTL, deadline: at() + 100 };
    assert.equal((await server.submit({ ...base, businessKey: '' })).reason, 'INVALID_KEY');
    assert.equal((await server.submit({ ...base, businessKey: 5 })).reason, 'INVALID_KEY');
    assert.equal((await server.submit({ ...base, businessKey: 'k', retryTtl: 0 })).reason, 'INVALID_DEADLINE');
    assert.equal((await server.submit({ ...base, businessKey: 'k', retryTtl: 1.5 })).reason, 'INVALID_DEADLINE');
    assert.equal((await server.submit({ ...base, businessKey: 'k', deadline: Infinity })).reason, 'INVALID_DEADLINE');
  });

  it('does not let a caller mutate stored parameters after submission', async () => {
    const { server, business, at } = setup();
    const params = { amount: 1 };
    await server.submit({ businessKey: 'mut', params, retryTtl: TTL, deadline: at() + 1_000 });
    params.amount = 999;

    const again = await server.submit({
      businessKey: 'mut',
      params: { amount: 1 },
      retryTtl: TTL,
      deadline: at() + 1_000,
    });
    assert.equal(again.status, 'effective');
    assert.equal(business.executeCalls.length, 1);
  });
});
