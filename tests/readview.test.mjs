import test from 'node:test';
import assert from 'node:assert/strict';

import { ReadView, evaluateBatch } from '../src/index.js';
import { SyntheticRunner } from '../src/fixtures.js';

const tree = { kind: 'scalar', sql: 'orders.total', args: [{ name: 'customer', param: 'customer' }] };

test('read-view reuse reports per-batch row indexes without mixing batches', async () => {
  const runner = new SyntheticRunner();
  const view = new ReadView(runner);

  const first = await view.evaluateBatch({
    tree,
    rows: [{ params: { customer: 'alice' } }, { params: { customer: 'bob' } }],
  });
  assert.equal(first.ok, true);
  assert.deepEqual(first.values, [100, 50]);
  assert.equal(first.stats.requestsIssued, 2);
  assert.deepEqual(first.stats.reusedFromEarlierBatches, []);

  const second = await view.evaluateBatch({
    tree,
    rows: [
      { params: { customer: 'bob' } },
      { params: { customer: 'alice' } },
      { params: { customer: 'carol' } },
    ],
  });
  assert.equal(second.ok, true);
  assert.deepEqual(second.values, [50, 100, 75]);
  assert.equal(second.stats.requestsIssued, 1, 'only carol is new');
  assert.equal(second.stats.cacheHits, 2);

  // The two reused entries must carry second-batch row indexes only.
  const reused = second.stats.reusedFromEarlierBatches;
  assert.equal(reused.length, 2);
  const byCustomer = Object.fromEntries(
    reused.map((entry) => [entry.args[0].value, entry.reusedByBatches]),
  );
  assert.deepEqual(byCustomer.alice, [{ batch: 2, rowIndexes: [1] }]);
  assert.deepEqual(byCustomer.bob, [{ batch: 2, rowIndexes: [0] }]);

  // And the newly issued request only mentions the second batch's row 2.
  assert.deepEqual(second.stats.perRequest[0].rowIndexes, [2]);
});
