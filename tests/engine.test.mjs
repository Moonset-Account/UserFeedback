import test from 'node:test';
import assert from 'node:assert/strict';

import { subqueryKey, CODES, evaluateBatch } from '../src/index.js';
import { SyntheticRunner, copyTables } from '../src/fixtures.js';

const scalarTotal = (param = 'customer') => ({
  kind: 'scalar',
  sql: 'orders.total',
  args: [{ name: 'customer', param }],
});

const ifNode = (cond, thenBranch, elseBranch) => ({
  kind: 'if',
  cond,
  then: thenBranch,
  else: elseBranch,
});

test('empty argument list is a valid, distinct key', () => {
  const key = subqueryKey('settings.version', []);
  assert.equal(key, JSON.stringify(['settings.version', []]));
  assert.equal(typeof key, 'string');
});

test('absent argument, present NULL, and empty args never collide', () => {
  const empty = subqueryKey('q', []);
  const missing = subqueryKey('q', [{ name: 'c', missing: true }]);
  const presentNull = subqueryKey('q', [{ name: 'c', value: null }]);
  const presentZero = subqueryKey('q', [{ name: 'c', value: 0 }]);
  const keys = new Set([empty, missing, presentNull, presentZero]);
  assert.equal(keys.size, 4);
});

test('argument order does not change the key, but the name does', () => {
  const ab = subqueryKey('q', [
    { name: 'a', value: 1 },
    { name: 'b', value: 2 },
  ]);
  const ba = subqueryKey('q', [
    { name: 'b', value: 2 },
    { name: 'a', value: 1 },
  ]);
  const ac = subqueryKey('q', [
    { name: 'a', value: 1 },
    { name: 'c', value: 2 },
  ]);
  assert.equal(ab, ba);
  assert.notEqual(ab, ac);
});

test('typed values are distinguished (string vs number vs boolean vs bigint)', () => {
  const keys = new Set([
    subqueryKey('q', [{ name: 'v', value: '1' }]),
    subqueryKey('q', [{ name: 'v', value: 1 }]),
    subqueryKey('q', [{ name: 'v', value: 1n }]),
    subqueryKey('q', [{ name: 'v', value: true }]),
  ]);
  assert.equal(keys.size, 4);
});

test('duplicate argument names are rejected', () => {
  assert.throws(
    () =>
      subqueryKey('q', [
        { name: 'a', value: 1 },
        { name: 'a', value: 2 },
      ]),
    (err) => err.code === CODES.DUPLICATE_KEY,
  );
});

test('a null argument and a present null match each other key-wise', () => {
  assert.equal(
    subqueryKey('q', [{ name: 'c', value: null }]),
    subqueryKey('q', [{ name: 'c', value: null }]),
  );
});

test('rows that share a request issue it once and keep projection order', async () => {
  const runner = new SyntheticRunner({ responseOrder: 'reverse' });
  const result = await evaluateBatch({
    tree: scalarTotal(),
    rows: [
      { params: { customer: 'alice' } },
      { params: { customer: 'alice' } },
      { params: { customer: 'bob' } },
    ],
    runner,
  });

  assert.equal(result.ok, true);
  assert.deepEqual(result.values, [100, 100, 50]);
  assert.equal(runner.issuedCount, 2);
  const alice = result.stats.perRequest.find((entry) => entry.args[0].value === 'alice');
  assert.deepEqual(alice.rowIndexes, [0, 1]);
});

test('duplicate outer rows are preserved, not deduplicated', async () => {
  const runner = new SyntheticRunner();
  const result = await evaluateBatch({
    tree: scalarTotal(),
    rows: [
      { params: { customer: 'alice' } },
      { params: { customer: 'alice' } },
      { params: { customer: 'alice' } },
    ],
    runner,
  });
  assert.equal(result.ok, true);
  assert.deepEqual(result.values, [100, 100, 100]);
  assert.equal(runner.issuedCount, 1);
});

test('zero rows yields NULL', async () => {
  const runner = new SyntheticRunner();
  const result = await evaluateBatch({
    tree: scalarTotal(),
    rows: [{ params: { customer: 'nobody-here' } }],
    runner,
  });
  assert.equal(result.ok, true);
  assert.deepEqual(result.values, [null]);
});

test('exactly one row yields that value', async () => {
  const runner = new SyntheticRunner();
  const result = await evaluateBatch({
    tree: scalarTotal(),
    rows: [{ params: { customer: 'bob' } }],
    runner,
  });
  assert.equal(result.ok, true);
  assert.deepEqual(result.values, [50]);
});

test('multiple rows yield a cardinality error at the original outer row', async () => {
  const runner = new SyntheticRunner();
  const result = await evaluateBatch({
    tree: { kind: 'scalar', sql: 'orders.dupe', args: [{ name: 'customer', param: 'customer' }] },
    rows: [
      { params: { customer: 'bob' } }, // orders.dupe has no bob: 0 rows -> NULL
      { params: { customer: 'alice' } }, // two alice rows -> cardinality error
    ],
    runner,
  });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, CODES.SCALAR_CARDINALITY);
  assert.equal(result.error.rowIndex, 1);
  assert.equal(result.values, undefined);
});

test('an unselected branch with a multi-row subquery does not affect the selected branch', async () => {
  const runner = new SyntheticRunner();
  const tree = ifNode(
    { kind: 'param', name: 'useDupe' },
    { kind: 'scalar', sql: 'orders.dupe', args: [{ name: 'customer', param: 'customer' }] },
    scalarTotal(),
  );
  const result = await evaluateBatch({
    tree,
    rows: [
      { params: { useDupe: false, customer: 'bob' } },
      { params: { useDupe: false, customer: 'alice' } },
    ],
    runner,
  });
  assert.equal(result.ok, true);
  assert.deepEqual(result.values, [50, 100]);
  assert.equal(runner.issuedCount, 2, 'bob and alice are distinct requests');
  assert.ok(
    result.stats.perRequest.every((entry) => entry.sql === 'orders.total'),
    'the multi-row orders.dupe query must never be issued from the pruned branch',
  );
  assert.deepEqual(
    result.stats.perRequest.map((entry) => entry.rowIndexes),
    [[0], [1]],
    'each row only demanded its own selected-branch request',
  );
});

test('IF prunes the unselected branch: no request is issued for it', async () => {
  const runner = new SyntheticRunner();
  const tree = ifNode(
    { kind: 'param', name: 'flag' },
    scalarTotal(),
    { kind: 'const', value: 7 },
  );
  const result = await evaluateBatch({
    tree,
    rows: [{ params: { flag: false, customer: 'alice' } }],
    runner,
  });
  assert.equal(result.ok, true);
  assert.deepEqual(result.values, [7]);
  assert.equal(runner.issuedCount, 0);
  assert.equal(runner.callCount, 0);
});

test('absent correlation key and NULL correlation key produce separate requests', async () => {
  const runner = new SyntheticRunner();
  const tree = {
    kind: 'scalar',
    sql: 'settings.by_tenant',
    args: [{ name: 'tenant', param: 'tenant' }],
  };
  const result = await evaluateBatch({
    tree,
    rows: [
      { params: {} }, // tenant absent in this row's environment
      { params: { tenant: null } }, // present, explicitly NULL
    ],
    runner,
  });
  assert.equal(result.ok, true);
  // Absent -> no row matches -> NULL. Present NULL -> matches the null row -> 9.
  assert.deepEqual(result.values, [null, 9]);
  assert.equal(runner.issuedCount, 2);
  const absent = result.stats.perRequest.find((entry) =>
    entry.args.some((arg) => arg.missing === true),
  );
  const presentNull = result.stats.perRequest.find((entry) =>
    entry.args.some((arg) => arg.name === 'tenant' && arg.value === null),
  );
  assert.ok(absent, 'expected a distinct request for the absent key');
  assert.ok(presentNull, 'expected a distinct request for the present NULL key');
  assert.notEqual(absent.key, presentNull.key);
});

test('missing correlation can be configured to raise an error instead', async () => {
  const runner = new SyntheticRunner();
  const result = await evaluateBatch({
    tree: scalarTotal(),
    rows: [{ params: {} }],
    runner,
    missingCorrelation: 'error',
  });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, CODES.MISSING_PARAM);
  assert.equal(result.error.rowIndex, 0);
});

test('out-of-order responses are matched by requestId, not by arrival position', async () => {
  const tree = {
    kind: 'add',
    left: scalarTotal(),
    right: {
      kind: 'scalar',
      sql: 'settings.version',
      args: [],
    },
  };
  for (const responseOrder of ['arrival', 'reverse', 'shuffle']) {
    const runner = new SyntheticRunner({ responseOrder, random: mulberry32(42) });
    const result = await evaluateBatch({
      tree,
      rows: [
        { params: { customer: 'alice' } },
        { params: { customer: 'bob' } },
      ],
      runner,
    });
    assert.equal(result.ok, true, `order=${responseOrder}`);
    assert.deepEqual(result.values, [107, 57], `order=${responseOrder}`);
  }
});

test('a batch error reports the earliest failing outer row only', async () => {
  const runner = new SyntheticRunner();
  // Row 0 hits a multi-row scalar; row 1 also would. Only row 0's error is reported.
  const result = await evaluateBatch({
    tree: { kind: 'scalar', sql: 'orders.dupe', args: [{ name: 'customer', param: 'customer' }] },
    rows: [
      { params: { customer: 'alice' } },
      { params: { customer: 'alice' } },
    ],
    runner,
  });
  assert.equal(result.ok, false);
  assert.equal(result.error.rowIndex, 0);
  // Both rows demand the same key, so one request serves both; the first served
  // demand is row 0's.
  assert.equal(runner.issuedCount, 1);
});

test('a later row failing keeps the earlier row failure position honest', async () => {
  const runner = new SyntheticRunner();
  const tree = {
    kind: 'div',
    left: { kind: 'const', value: 10 },
    right: { kind: 'param', name: 'denominator' },
  };
  const result = await evaluateBatch({
    tree,
    rows: [
      { params: { denominator: 2 } },
      { params: { denominator: 0 } },
      { params: { denominator: 5 } },
    ],
    runner,
  });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, CODES.DIV_BY_ZERO);
  assert.equal(result.error.rowIndex, 1);
  assert.equal(result.values, undefined, 'no partial results on failure');
});

test('earlier row failure wins even when a later row also fails', async () => {
  const runner = new SyntheticRunner();
  const tree = {
    kind: 'div',
    left: { kind: 'const', value: 10 },
    right: { kind: 'param', name: 'denominator' },
  };
  const result = await evaluateBatch({
    tree,
    rows: [
      { params: { denominator: 0 } },
      { params: { denominator: 0 } },
    ],
    runner,
  });
  assert.equal(result.ok, false);
  assert.equal(result.error.rowIndex, 0);
});

test('rows after the reported error are not evaluated', async () => {
  const runner = new SyntheticRunner();
  const tree = ifNode(
    { kind: 'param', name: 'fail' },
    { kind: 'error', code: CODES.TYPE, reason: 'boom' },
    scalarTotal(),
  );
  const result = await evaluateBatch({
    tree,
    rows: [
      { params: { fail: true, customer: 'alice' } },
      { params: { fail: false, customer: 'bob' } },
    ],
    runner,
  });
  assert.equal(result.ok, false);
  assert.equal(result.error.rowIndex, 0);
  assert.equal(result.error.reason, 'boom');
  assert.equal(runner.issuedCount, 0, 'row 1 must not issue a query after row 0 failed');
});

test('a fresh read view does not reuse a previous batch cache (data may change)', async () => {
  const { ReadView } = await import('../src/index.js');
  const tables = copyTables();
  const runner = new SyntheticRunner({ tables });
  const view = new ReadView(runner);

  const tree = scalarTotal();
  const rows = [{ params: { customer: 'alice' } }];
  const first = await view.evaluateBatch({ tree, rows });
  assert.deepEqual(first.values, [100]);

  // Mutate the underlying "database" between views.
  tables['orders.total'] = [{ customer: 'alice', total: 999 }];

  const second = await view.evaluateBatch({ tree, rows });
  assert.deepEqual(second.values, [100], 'same read view must serve the cached value');
  assert.equal(second.stats.reusedFromEarlierBatches.length, 1);

  const freshRunner = new SyntheticRunner({ tables });
  const freshView = new ReadView(freshRunner);
  const third = await freshView.evaluateBatch({ tree, rows });
  assert.deepEqual(third.values, [999], 'new read view must observe new data');
});

test('the same read view reuses an identical request across batches', async () => {
  const { ReadView } = await import('../src/index.js');
  const runner = new SyntheticRunner();
  const view = new ReadView(runner);
  const tree = scalarTotal();

  const first = await view.evaluateBatch({
    tree,
    rows: [{ params: { customer: 'alice' } }],
  });
  const second = await view.evaluateBatch({
    tree,
    rows: [{ params: { customer: 'alice' } }],
  });

  assert.deepEqual(first.values, [100]);
  assert.deepEqual(second.values, [100]);
  assert.equal(first.stats.requestsIssued, 1);
  assert.equal(second.stats.requestsIssued, 0);
  assert.equal(second.stats.cacheHits, 1);
  assert.equal(runner.issuedCount, 1);
});

test('a runner-reported query error is attributed to every demanding row at its own position', async () => {
  const runner = new SyntheticRunner({
    override: (request) =>
      request.sql === 'unstable.flaky'
        ? { error: { code: 'DB_TIMEOUT', reason: 'synthetic timeout' } }
        : null,
  });
  const result = await evaluateBatch({
    tree: { kind: 'scalar', sql: 'unstable.flaky', args: [{ name: 'customer', param: 'customer' }] },
    rows: [
      { params: { customer: 'alice' } },
      { params: { customer: 'alice' } },
    ],
    runner,
  });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, 'DB_TIMEOUT');
  assert.equal(result.error.rowIndex, 0);
  assert.equal(runner.issuedCount, 1);
});

test('malformed runner responses are rejected instead of silently used', async () => {
  const badCases = [
    { name: 'not an array', response: () => ({}) },
    { name: 'unknown requestId', response: () => [{ requestId: 99, rows: [] }] },
    { name: 'duplicate requestId', response: () => [
      { requestId: 1, rows: [] },
      { requestId: 1, rows: [] },
    ] },
    { name: 'missing response', response: () => [] },
    { name: 'rows not array', response: () => [{ requestId: 1, rows: 'nope' }] },
    { name: 'multi-column row', response: () => [{ requestId: 1, rows: [[1, 2]] }] },
    { name: 'both rows and error', response: () => [
      { requestId: 1, rows: [], error: { code: 'X' } },
    ] },
    { name: 'neither rows nor error', response: () => [{ requestId: 1 }] },
  ];

  for (const item of badCases) {
    const runner = { resolve: async () => item.response() };
    const result = await evaluateBatch({
      tree: scalarTotal(),
      rows: [{ params: { customer: 'alice' } }],
      runner,
    });
    assert.equal(result.ok, false, item.name);
    assert.equal(result.error.code, CODES.BAD_RESPONSE, item.name);
  }
});

test('a throwing runner surfaces as a RUNNER error with no partial values', async () => {
  const runner = {
    resolve: async () => {
      throw new Error('transport closed');
    },
  };
  const result = await evaluateBatch({
    tree: scalarTotal(),
    rows: [{ params: { customer: 'alice' } }],
    runner,
  });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, CODES.RUNNER);
  assert.equal(result.values, undefined);
});

test('empty batch returns empty projection without calling the runner', async () => {
  const runner = new SyntheticRunner();
  const result = await evaluateBatch({ tree: scalarTotal(), rows: [], runner });
  assert.equal(result.ok, true);
  assert.deepEqual(result.values, []);
  assert.equal(runner.callCount, 0);
});

test('two subqueries in one row issue in one merged batch', async () => {
  const runner = new SyntheticRunner({ responseOrder: 'reverse' });
  const tree = {
    kind: 'add',
    left: scalarTotal(),
    right: scalarTotal('customer'),
  };
  const result = await evaluateBatch({
    tree,
    rows: [{ params: { customer: 'bob' } }],
    runner,
  });
  assert.equal(result.ok, true);
  assert.deepEqual(result.values, [100], '50 + 50');
  assert.equal(runner.callCount, 1, 'one merged resolve() call');
  assert.equal(runner.issuedCount, 1, 'both nodes demand the same key');
});

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
