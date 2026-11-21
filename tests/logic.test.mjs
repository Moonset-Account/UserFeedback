import { test } from 'node:test';
import assert from 'node:assert/strict';
import { compile } from '../src/index.js';

const col = (name) => ({ kind: 'column', name });
const lit = (value) => ({ kind: 'literal', value });
const cmp = (op, left, right) => ({ kind: 'compare', op, left, right });
const and = (left, right) => ({ kind: 'and', left, right });
const or = (left, right) => ({ kind: 'or', left, right });

function run(expr, columns, selectedRows, validity) {
  return compile(expr).execute({ columns, selectedRows, validity });
}

test('selected rows only, in original selection order (incl. duplicates and reordering)', () => {
  const expr = col('a');
  const { outputs, errors, results } = run(
    expr,
    { a: [10, 20, 30, 40] },
    [3, 0, 3, 1],
  );
  assert.deepEqual(outputs, [
    { row: 3, value: 40 },
    { row: 0, value: 10 },
    { row: 3, value: 40 },
    { row: 1, value: 20 },
  ]);
  assert.deepEqual(errors, []);
  assert.deepEqual(results.map((r) => r.row), [3, 0, 3, 1]);
  assert.deepEqual(results.map((r) => r.status), ['value', 'value', 'value', 'value']);
});

test('CASE enters a branch only when its condition is TRUE; NULL and false fall through', () => {
  // CASE WHEN a > 5 THEN 1 WHEN b > 0 THEN 2 ELSE 3 END
  const expr = {
    kind: 'case',
    branches: [
      { when: cmp('gt', col('a'), lit(5)), then: lit(1) },
      { when: cmp('gt', col('b'), lit(0)), then: lit(2) },
    ],
    else: lit(3),
  };
  // rows: 0 first cond true; 1 first cond false, second true;
  //       2 first cond NULL (a null), second false; 3 both NULL
  const columns = {
    a: [10, 0, null, null],
    b: [99, 7, -1, null],
  };
  const { outputs } = run(expr, columns, [0, 1, 2, 3]);
  assert.deepEqual(outputs.map((o) => o.value), [1, 2, 3, 3]);
});

test('CASE without ELSE returns NULL when no condition matches (empty condition -> other branch)', () => {
  const expr = {
    kind: 'case',
    branches: [{ when: cmp('gt', col('a'), lit(100)), then: lit(1) }],
  };
  const { outputs } = run(expr, { a: [1, 200, null] }, [0, 1, 2]);
  assert.deepEqual(outputs.map((o) => o.value), [null, 1, null]);
});

test('AND short-circuits on false left: right branch with div-by-zero is never evaluated', () => {
  // (a > 10) AND (1 / b = 1)  -- row 1: left false, b=0 must not raise
  const right = cmp('eq', { kind: 'div', left: lit(1), right: col('b') }, lit(1));
  const expr = and(cmp('gt', col('a'), lit(10)), right);
  const { outputs, errors } = run(
    expr,
    { a: [20, 0], b: [1, 0] },
    [0, 1],
  );
  assert.deepEqual(outputs, [
    { row: 0, value: true },
    { row: 1, value: false },
  ]);
  assert.deepEqual(errors, []);
});

test('OR short-circuits on true left: right branch with div-by-zero is never evaluated', () => {
  // Row 0: left is true and b=0. If the right side ran it would raise
  // DIVISION_BY_ZERO; short-circuiting means the row succeeds with true.
  const expr = or(
    cmp('gt', col('a'), lit(10)),
    cmp('eq', { kind: 'div', left: lit(1), right: col('b') }, lit(1)),
  );
  const shortCircuited = run(expr, { a: [20, 0], b: [0, 0] }, [0]);
  assert.deepEqual(shortCircuited.outputs, [{ row: 0, value: true }]);
  assert.deepEqual(shortCircuited.errors, []);

  // Control: the same row with a false left side does reach the division
  // and must be rejected, proving the success above really came from
  // short-circuit evaluation rather than the division being ignored.
  const notShortCircuited = run(expr, { a: [20, 0], b: [0, 0] }, [1]);
  assert.deepEqual(notShortCircuited.outputs, []);
  assert.equal(notShortCircuited.errors[0].row, 1);
  assert.equal(notShortCircuited.errors[0].code, 'DIVISION_BY_ZERO');
});

test('SQL three-valued logic for AND when the left side does not short-circuit', () => {
  // NULL AND false -> false ; NULL AND true -> NULL ; NULL AND NULL -> NULL
  const columns = { a: [null, null, null], b: [false, true, null] };
  const expr = and(col('a'), col('b'));
  const { outputs } = run(expr, columns, [0, 1, 2]);
  assert.deepEqual(outputs.map((o) => o.value), [false, null, null]);
});

test('SQL three-valued logic for OR when the left side does not short-circuit', () => {
  // NULL OR true -> true ; NULL OR false -> NULL ; NULL OR NULL -> NULL
  const columns = { a: [null, null, null], b: [true, false, null] };
  const expr = or(col('a'), col('b'));
  const { outputs } = run(expr, columns, [0, 1, 2]);
  assert.deepEqual(outputs.map((o) => o.value), [true, null, null]);
});

test('comparisons with NULL operand yield NULL, including all six integer operators', () => {
  const ops = ['eq', 'neq', 'lt', 'lte', 'gt', 'gte'];
  const columns = { a: [null, 5], b: [5, null] };
  for (const op of ops) {
    const { outputs } = run(cmp(op, col('a'), col('b')), columns, [0, 1]);
    assert.deepEqual(outputs.map((o) => o.value), [null, null], `op=${op}`);
  }
});

test('integer comparisons compute ordinary boolean results on non-null rows', () => {
  const expr = cmp('gte', col('a'), col('b'));
  const { outputs } = run(expr, { a: [3, 2, 1], b: [1, 2, 3] }, [0, 1, 2]);
  assert.deepEqual(outputs.map((o) => o.value), [true, true, false]);
});
