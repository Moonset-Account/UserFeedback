/**
 * Edge and interleaving tests that were called out explicitly in the task:
 *
 *  - empty-argument requests merge across rows
 *  - an unselected branch that would produce a multi-row result is harmless
 *  - an earlier row's error, only discoverable after its subquery resolves,
 *    still beats a later row's locally-detected error
 *  - the batch reports exactly one error and no partial projection
 *  - out-of-order responses never reorder the projection
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { evaluateBatch, ReferenceInterpreter, CODES } from '../src/index.js';
import { SyntheticRunner, copyTables } from '../src/fixtures.js';

const emptyArgScalar = { kind: 'scalar', sql: 'settings.version', args: [] };

test('empty-argument requests are merged across rows within one read view', async () => {
  const runner = new SyntheticRunner();
  const result = await evaluateBatch({
    tree: emptyArgScalar,
    rows: [{ params: {} }, { params: {} }, { params: {} }],
    runner,
  });
  assert.equal(result.ok, true);
  assert.deepEqual(result.values, [7, 7, 7]);
  assert.equal(runner.issuedCount, 1);
  assert.deepEqual(result.stats.perRequest[0].args, []);
  assert.deepEqual(result.stats.perRequest[0].rowIndexes, [0, 1, 2]);
});

test('empty args, absent field, and NULL field stay three distinct requests in one batch', async () => {
  const runner = new SyntheticRunner();
  const tree = {
    kind: 'coalesce',
    items: [
      { kind: 'scalar', sql: 'settings.by_tenant', args: [{ name: 'tenant', param: 'tenant' }] },
      { kind: 'scalar', sql: 'settings.version', args: [] },
    ],
  };
  const result = await evaluateBatch({
    tree,
    rows: [
      { params: {} }, // absent tenant
      { params: { tenant: null } }, // present NULL
      { params: {} }, // absent again -> same key as row 0
    ],
    runner,
  });
  assert.equal(result.ok, true);
  // absent -> settings.by_tenant matches nothing -> coalesce falls back to version 7
  // present NULL -> matches the null tenant row -> 9
  assert.deepEqual(result.values, [7, 9, 7]);
  assert.equal(
    runner.issuedCount,
    3,
    'absent tenant, present-NULL tenant, and empty-arg version are three distinct keys',
  );
  const keys = result.stats.perRequest.map((entry) => entry.key);
  assert.equal(new Set(keys).size, 3);
  assert.deepEqual(
    result.stats.perRequest.map((entry) => entry.rowIndexes).sort((a, b) => a[0] - b[0]),
    [[0, 2], [0, 2], [1]],
  );
});

test('unselected branch with a multi-row scalar does not poison the selected branch', async () => {
  const runner = new SyntheticRunner();
  const tree = {
    kind: 'if',
    cond: { kind: 'param', name: 'pickSafe' },
    then: { kind: 'scalar', sql: 'orders.total', args: [{ name: 'customer', param: 'customer' }] },
    else: { kind: 'scalar', sql: 'orders.dupe', args: [{ name: 'customer', param: 'customer' }] },
  };
  const result = await evaluateBatch({
    tree,
    rows: [
      { params: { pickSafe: true, customer: 'alice' } },
      { params: { pickSafe: true, customer: 'bob' } },
    ],
    runner,
  });
  assert.equal(result.ok, true);
  assert.deepEqual(result.values, [100, 50]);
  assert.ok(
    result.stats.perRequest.every((entry) => entry.sql === 'orders.total'),
    'the dupe branch must never be issued',
  );
});

test('unselected branch holding an explicit error node never fires', async () => {
  const runner = new SyntheticRunner();
  const tree = {
    kind: 'if',
    cond: { kind: 'param', name: 'flag' },
    then: { kind: 'error', code: CODES.TYPE, reason: 'should not run' },
    else: { kind: 'const', value: 'ok' },
  };
  const result = await evaluateBatch({
    tree,
    rows: [{ params: { flag: false } }, { params: { flag: false } }],
    runner,
  });
  assert.equal(result.ok, true);
  assert.deepEqual(result.values, ['ok', 'ok']);
  assert.equal(runner.callCount, 0);
});

test('earlier row error discovered after its subquery resolves beats a later local error', async () => {
  // Row 0 suspends on a subquery, then (after resolving) divides by zero.
  // Row 1 fails locally without any subquery in the same first pass.
  // The batch must report row 0, not row 1.
  const runner = new SyntheticRunner();
  const tree = {
    kind: 'add',
    left: {
      kind: 'scalar',
      sql: 'orders.total',
      args: [{ name: 'customer', param: 'customer' }],
    },
    right: {
      kind: 'div',
      left: { kind: 'const', value: 0 },
      right: { kind: 'param', name: 'divisor' },
    },
  };
  const result = await evaluateBatch({
    tree,
    rows: [
      { params: { customer: 'alice', divisor: 0 } }, // scalar ok, then div by zero
      { params: { customer: 'bob', divisor: 0 } }, // div by zero, no scalar
    ],
    runner,
  });

  assert.equal(result.ok, false);
  assert.equal(result.error.rowIndex, 0, 'row 0 is the earliest valid error');
  assert.equal(result.error.code, CODES.DIV_BY_ZERO);
  assert.deepEqual(result.error.path, ['right']);
  assert.equal(result.values, undefined, 'no partial projection');
});

test('third row cardinality error is reported at original row 2, rows 0/1 discarded', async () => {
  const runner = new SyntheticRunner();
  const tree = {
    kind: 'scalar',
    sql: 'orders.by_region',
    args: [{ name: 'region', param: 'region' }],
  };
  const result = await evaluateBatch({
    tree,
    rows: [
      { params: { region: 'south' } }, // one row -> 20
      { params: { region: 'south' } }, // shared key -> 20
      { params: { region: 'north' } }, // two rows -> cardinality error
    ],
    runner,
  });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, CODES.SCALAR_CARDINALITY);
  assert.equal(result.error.rowIndex, 2);
  assert.equal(result.error.detail.rowCount, 2);
  assert.equal(result.values, undefined);
});

test('out-of-order and shuffled responses keep the original projection order', async () => {
  const tree = {
    kind: 'add',
    left: { kind: 'scalar', sql: 'orders.total', args: [{ name: 'customer', param: 'customer' }] },
    right: { kind: 'scalar', sql: 'settings.version', args: [] },
  };
  const rows = [
    { params: { customer: 'carol' } },
    { params: { customer: 'alice' } },
    { params: { customer: 'carol' } },
    { params: { customer: 'bob' } },
  ];
  const expected = [82, 107, 82, 57];

  for (const responseOrder of ['arrival', 'reverse', 'shuffle']) {
    const runner = new SyntheticRunner({ responseOrder, random: seeded(7) });
    const result = await evaluateBatch({ tree, rows, runner });
    assert.equal(result.ok, true, responseOrder);
    assert.deepEqual(result.values, expected, responseOrder);
    assert.equal(runner.issuedCount, 4, `${responseOrder}: 3 customers + 1 empty-arg`);
  }
});

test('batched result agrees with the naive interpreter on a mixed batch', async () => {
  const tree = {
    kind: 'if',
    cond: { kind: 'eq', left: { kind: 'param', name: 'shape' }, right: { kind: 'const', value: 'sum' } },
    then: {
      kind: 'add',
      left: { kind: 'scalar', sql: 'orders.total', args: [{ name: 'customer', param: 'customer' }] },
      right: { kind: 'scalar', sql: 'settings.version', args: [] },
    },
    else: { kind: 'const', value: 0 },
  };
  const rows = [
    { params: { shape: 'sum', customer: 'alice' } },
    { params: { shape: 'none', customer: 'alice' } },
    { params: { shape: 'sum', customer: 'alice' } },
    { params: { shape: 'sum', customer: 'carol' } },
  ];

  const batchedRunner = new SyntheticRunner({ tables: copyTables(), responseOrder: 'reverse' });
  const batched = await evaluateBatch({ tree, rows, runner: batchedRunner });

  const refRunner = new SyntheticRunner({ tables: copyTables(), responseOrder: 'reverse' });
  const reference = new ReferenceInterpreter(refRunner);
  const ref = await reference.run({ tree, rows });

  assert.equal(batched.ok, true);
  assert.deepEqual(batched.values, ref.values);
  assert.deepEqual(batched.values, [107, 0, 107, 82]);
  assert.equal(batchedRunner.issuedCount, 3, 'alice, carol + empty-arg version');
  assert.equal(
    refRunner.issuedCount,
    6,
    'reference re-issues both scalars for each of the three sum-branch rows',
  );
});

function seeded(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
