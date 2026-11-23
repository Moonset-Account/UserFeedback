/**
 * Differential tests: the batched engine vs the naive row-by-row interpreter.
 *
 * These are the property-style cross-checks. For several scenarios the two
 * implementations must agree on:
 *   - the projected values (order and duplicates included)
 *   - the reported error position (row index, path, code), or success
 *   - the wire request payloads (batched issues the same set of distinct
 *     requests as the reference, but fewer total calls when requests repeat)
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { evaluateBatch, ReferenceInterpreter, ReadView } from '../src/index.js';
import { SyntheticRunner, copyTables } from '../src/fixtures.js';

function normalizeValue(value) {
  return value === undefined ? '__undefined__' : value;
}

async function compare({ name, tree, rows, missingCorrelation = 'mark-missing' }) {
  const batchedRunner = new SyntheticRunner({ tables: copyTables(), responseOrder: 'reverse' });
  const batched = await evaluateBatch({ tree, rows, missingCorrelation, runner: batchedRunner });

  const refRunner = new SyntheticRunner({ tables: copyTables(), responseOrder: 'reverse' });
  const reference = new ReferenceInterpreter(refRunner);
  const ref = await reference.run({ tree, rows, missingCorrelation });

  assert.equal(batched.ok, ref.ok, `${name}: ok flags differ`);

  if (batched.ok) {
    assert.deepEqual(
      batched.values.map(normalizeValue),
      ref.values.map(normalizeValue),
      `${name}: projected values differ`,
    );
  } else {
    assert.equal(batched.error.code, ref.error.code, `${name}: error code differs`);
    assert.equal(batched.error.rowIndex, ref.error.rowIndex, `${name}: error rowIndex differs`);
    assert.deepEqual(batched.error.path, ref.error.path, `${name}: error path differs`);
    assert.equal(batched.values, undefined, `${name}: failed batch must not expose values`);
  }

  // Same distinct request set.
  const batchedKeys = new Set(
    (batched.stats.perRequest ?? []).map((entry) => JSON.stringify([entry.sql, entry.args])),
  );
  const refKeys = new Set(reference.calls.map((call) => JSON.stringify([call.sql, call.args])));
  assert.deepEqual(
    [...batchedKeys].sort(),
    [...refKeys].sort(),
    `${name}: request payload sets differ`,
  );

  // Batched never issues more than the reference; equal when nothing repeats.
  assert.ok(
    batchedRunner.issuedCount <= refRunner.issuedCount,
    `${name}: batched issued ${batchedRunner.issuedCount}, reference ${refRunner.issuedCount}`,
  );

  return { batched, reference, batchedRunner, refRunner };
}

test('differential: simple correlated scalar over three rows', async () => {
  await compare({
    name: 'simple',
    tree: { kind: 'scalar', sql: 'orders.total', args: [{ name: 'customer', param: 'customer' }] },
    rows: [
      { params: { customer: 'alice' } },
      { params: { customer: 'bob' } },
      { params: { customer: 'carol' } },
    ],
  });
});

test('differential: repeated keys merge in batched, not in reference', async () => {
  const { batchedRunner, refRunner } = await compare({
    name: 'repeats',
    tree: { kind: 'scalar', sql: 'orders.total', args: [{ name: 'customer', param: 'customer' }] },
    rows: [
      { params: { customer: 'alice' } },
      { params: { customer: 'alice' } },
      { params: { customer: 'carol' } },
      { params: { customer: 'alice' } },
    ],
  });
  assert.equal(batchedRunner.issuedCount, 2);
  assert.equal(refRunner.issuedCount, 4);
});

test('differential: IF prunes branches per row', async () => {
  const tree = {
    kind: 'if',
    cond: { kind: 'param', name: 'flag' },
    then: { kind: 'scalar', sql: 'orders.total', args: [{ name: 'customer', param: 'customer' }] },
    else: { kind: 'const', value: -1 },
  };
  const { batchedRunner } = await compare({
    name: 'prune',
    tree,
    rows: [
      { params: { flag: true, customer: 'alice' } },
      { params: { flag: false, customer: 'bob' } },
      { params: { flag: true, customer: 'alice' } },
    ],
  });
  assert.equal(batchedRunner.issuedCount, 1);
});

test('differential: unselected branch with multi-row result stays harmless', async () => {
  const tree = {
    kind: 'if',
    cond: { kind: 'param', name: 'useDupe' },
    then: { kind: 'scalar', sql: 'orders.dupe', args: [{ name: 'customer', param: 'customer' }] },
    else: { kind: 'scalar', sql: 'orders.total', args: [{ name: 'customer', param: 'customer' }] },
  };
  await compare({
    name: 'prune-multirow',
    tree,
    rows: [
      { params: { useDupe: false, customer: 'alice' } },
      { params: { useDupe: false, customer: 'bob' } },
    ],
  });
});

test('differential: absent vs NULL correlation keys diverge correctly', async () => {
  await compare({
    name: 'absent-null',
    tree: { kind: 'scalar', sql: 'settings.by_tenant', args: [{ name: 'tenant', param: 'tenant' }] },
    rows: [
      { params: {} },
      { params: { tenant: null } },
      { params: { tenant: 'acme' } },
      { params: {} },
    ],
  });
});

test('differential: cardinality error position', async () => {
  await compare({
    name: 'cardinality',
    tree: { kind: 'scalar', sql: 'orders.dupe', args: [{ name: 'customer', param: 'customer' }] },
    rows: [
      { params: { customer: 'bob' } },
      { params: { customer: 'alice' } },
    ],
  });
});

test('differential: error in an earlier sub-expression of a later row', async () => {
  const tree = {
    kind: 'add',
    left: {
      kind: 'div',
      left: { kind: 'const', value: 1 },
      right: { kind: 'param', name: 'divisor' },
    },
    right: { kind: 'scalar', sql: 'orders.total', args: [{ name: 'customer', param: 'customer' }] },
  };
  await compare({
    name: 'earlier-failure',
    tree,
    rows: [
      { params: { divisor: 1, customer: 'alice' } },
      { params: { divisor: 1, customer: 'alice' } },
      { params: { divisor: 0, customer: 'bob' } },
    ],
  });
});

test('differential: NULL propagation through arithmetic', async () => {
  const tree = {
    kind: 'add',
    left: { kind: 'scalar', sql: 'orders.total', args: [{ name: 'customer', param: 'customer' }] },
    right: { kind: 'const', value: 1 },
  };
  const { batched } = await compare({
    name: 'null-propagation',
    tree,
    rows: [
      { params: { customer: 'nobody-here' } }, // NULL + 1 = NULL
      { params: { customer: 'bob' } },
    ],
  });
  assert.deepEqual(batched.values, [null, 51]);
});

test('differential: empty batch', async () => {
  const tree = { kind: 'scalar', sql: 'orders.total', args: [{ name: 'customer', param: 'customer' }] };
  const batchedRunner = new SyntheticRunner();
  const batched = await evaluateBatch({ tree, rows: [], runner: batchedRunner });
  const refRunner = new SyntheticRunner();
  const reference = new ReferenceInterpreter(refRunner);
  const ref = await reference.run({ tree, rows: [] });
  assert.equal(batched.ok, true);
  assert.deepEqual(batched.values, ref.values);
  assert.equal(batchedRunner.callCount, 0);
  assert.equal(refRunner.callCount, 0);
});

test('interleaved: two read views over changing data do not share results', async () => {
  const tables = copyTables();
  const tree = { kind: 'scalar', sql: 'orders.total', args: [{ name: 'customer', param: 'customer' }] };
  const rows = [{ params: { customer: 'alice' } }];

  const runnerA = new SyntheticRunner({ tables });
  const viewA = new ReadView(runnerA);
  assert.deepEqual((await viewA.evaluateBatch({ tree, rows })).values, [100]);

  // Mutate the synthetic store, then run an interleaved, independent view.
  tables['orders.total'] = [{ customer: 'alice', total: 500 }];

  const runnerB = new SyntheticRunner({ tables });
  const viewB = new ReadView(runnerB);
  assert.deepEqual((await viewB.evaluateBatch({ tree, rows })).values, [500]);

  // A's cache still serves the old value: same read view, stable snapshot.
  assert.deepEqual((await viewA.evaluateBatch({ tree, rows })).values, [100]);

  // And the batched-vs-reference differential holds for each view's data.
  const refRunner = new SyntheticRunner({ tables });
  const reference = new ReferenceInterpreter(refRunner);
  const ref = await reference.run({ tree, rows });
  assert.deepEqual(ref.values, [500]);
});

test('interleaved: multiple awaits between rows do not break requestId mapping', async () => {
  // A runner whose responses arrive in a random order and whose resolve() is
  // async with a variable delay. The engine must map by requestId regardless.
  const tables = copyTables();
  let call = 0;
  const runner = {
    async resolve({ requests }) {
      call += 1;
      await new Promise((resolve) => setTimeout(resolve, call % 2));
      const inner = new SyntheticRunner({ tables });
      const responses = requests.map((request) => ({
        requestId: request.requestId,
        rows: inner.lookup(request),
      }));
      return responses.reverse();
    },
  };

  const tree = {
    kind: 'add',
    left: { kind: 'scalar', sql: 'orders.total', args: [{ name: 'customer', param: 'customer' }] },
    right: { kind: 'scalar', sql: 'settings.version', args: [] },
  };
  const result = await evaluateBatch({
    tree,
    rows: [
      { params: { customer: 'alice' } },
      { params: { customer: 'bob' } },
      { params: { customer: 'carol' } },
    ],
    runner,
  });
  assert.equal(result.ok, true);
  assert.deepEqual(result.values, [107, 57, 82]);
});
