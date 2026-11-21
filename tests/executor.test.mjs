import { test } from 'node:test';
import assert from 'node:assert/strict';
import { compile, ExpressionError } from '../src/index.js';

const col = (name) => ({ kind: 'column', name });
const lit = (value) => ({ kind: 'literal', value });
const cmp = (op, left, right) => ({ kind: 'compare', op, left, right });
const and = (left, right) => ({ kind: 'and', left, right });
const or = (left, right) => ({ kind: 'or', left, right });
const div = (left, right) => ({ kind: 'div', left, right });

// The exact same sub-expression node object is referenced from two CASE
// branches. Rows taking the first branch must never pay for (or error on)
// the shared division used only by the second branch.
const sharedDivision = div(lit(100), col('b'));
const sharedExpr = {
  kind: 'case',
  branches: [
    { when: cmp('gt', col('a'), lit(0)), then: lit(0) },
    { when: cmp('eq', col('a'), lit(0)), then: sharedDivision },
  ],
  else: lit(-1),
};

test('shared sub-expression reused across branches does not raise on unvisited rows', () => {
  // b=0 for every row, so 100/b always divides by zero. Only rows that
  // actually enter the second branch may see that error.
  const columns = { a: [5, 0, -3, 0], b: [0, 0, 0, 0] };
  const { outputs, errors, results } = compile(sharedExpr).execute({
    columns,
    selectedRows: [0, 1, 2, 3],
  });

  assert.deepEqual(outputs, [
    { row: 0, value: 0 },   // first branch
    { row: 2, value: -1 },  // ELSE
  ]);
  assert.deepEqual(errors.map((e) => e.row), [1, 3]);
  assert.ok(errors.every((e) => e.code === 'DIVISION_BY_ZERO'));
  assert.deepEqual(results.map((r) => [r.row, r.status]), [
    [0, 'value'],
    [1, 'error'],
    [2, 'value'],
    [3, 'error'],
  ]);
});

test('reusing the compiled expression on a new batch never carries values over', () => {
  const executor = compile(sharedExpr);

  const first = executor.execute({
    columns: { a: [0], b: [2] }, // second branch: 100/2 = 50
    selectedRows: [0],
  });
  assert.deepEqual(first.outputs, [{ row: 0, value: 50 }]);
  assert.deepEqual(first.errors, []);

  // Same row number, different batch values: result must come from the new
  // buffers (b=0 -> division by zero), not from the previous batch's 50.
  const second = executor.execute({
    columns: { a: [0], b: [0] },
    selectedRows: [0],
  });
  assert.deepEqual(second.outputs, []);
  assert.equal(second.errors.length, 1);
  assert.equal(second.errors[0].row, 0);
  assert.equal(second.errors[0].code, 'DIVISION_BY_ZERO');

  // A third batch with different column data again evaluates independently.
  const third = executor.execute({
    columns: { a: [7], b: [4] }, // first branch: constant 0, b unused
    selectedRows: [0],
  });
  assert.deepEqual(third.outputs, [{ row: 0, value: 0 }]);
  assert.deepEqual(third.errors, []);
});

test('interleaved success and error rows are both returned with original row numbers', () => {
  // (a >= 0) AND (10 / b >= 0)
  const expr = and(
    cmp('gte', col('a'), lit(0)),
    cmp('gte', div(lit(10), col('b')), lit(0)),
  );
  const columns = {
    a: [1, 1, -1, 1, 1],
    b: [2, 0, 0, 5, 0],
  };
  const { outputs, errors, results } = compile(expr).execute({
    columns,
    selectedRows: [0, 1, 2, 3, 4],
  });

  // Row 2: left side false -> short circuit, no division error despite b=0.
  assert.deepEqual(outputs, [
    { row: 0, value: true },
    { row: 2, value: false },
    { row: 3, value: true },
  ]);
  assert.deepEqual(errors.map((e) => e.row), [1, 4]);
  assert.deepEqual(
    results.map((r) => ({ row: r.row, status: r.status })),
    [
      { row: 0, status: 'value' },
      { row: 1, status: 'error' },
      { row: 2, status: 'value' },
      { row: 3, status: 'value' },
      { row: 4, status: 'error' },
    ],
  );
});

test('NULL condition inside CASE combined with short-circuiting AND (cross constraint)', () => {
  // CASE WHEN (a > 0 AND c > 0) THEN 10/b ELSE -1 END
  const expr = {
    kind: 'case',
    branches: [
      {
        when: and(cmp('gt', col('a'), lit(0)), cmp('gt', col('c'), lit(0))),
        then: div(lit(10), col('b')),
      },
    ],
    else: lit(-1),
  };
  const columns = [
    // row 0: cond true, b=2 -> 5
    { a: 1, b: 2, c: 1 },
    // row 1: cond false (a=0), b=0 -> ELSE, division never runs
    { a: 0, b: 0, c: 1 },
    // row 2: cond NULL (c null), b=0 -> ELSE, division never runs
    { a: 1, b: 0, c: null },
    // row 3: cond true, b=0 -> enters branch, division error with row 3
    { a: 1, b: 0, c: 1 },
  ].reduce((acc, row, i) => {
    for (const key of Object.keys(row)) {
      (acc[key] ??= [])[i] = row[key];
    }
    return acc;
  }, {});

  const { outputs, errors } = compile(expr).execute({
    columns,
    selectedRows: [0, 1, 2, 3],
  });
  assert.deepEqual(outputs, [
    { row: 0, value: 5 },
    { row: 1, value: -1 },
    { row: 2, value: -1 },
  ]);
  assert.deepEqual(errors, [
    {
      row: 3,
      code: 'DIVISION_BY_ZERO',
      message: 'Division by zero',
      details: { numerator: 10 },
    },
  ]);
});

test('out-of-range selected rows are row-level errors and do not abort the batch', () => {
  const executor = compile(col('a'));
  const { outputs, errors, results } = executor.execute({
    columns: { a: [10, 20] },
    selectedRows: [1, 9, -1, 0],
  });
  assert.deepEqual(outputs, [
    { row: 1, value: 20 },
    { row: 0, value: 10 },
  ]);
  assert.deepEqual(errors.map((e) => [e.row, e.code]), [
    [9, 'ROW_OUT_OF_RANGE'],
    [-1, 'ROW_OUT_OF_RANGE'],
  ]);
  assert.deepEqual(results.map((r) => r.status), ['value', 'error', 'error', 'value']);
});

test('empty selection returns empty success and error collections', () => {
  const result = compile(div(col('a'), col('b'))).execute({
    columns: { a: [1], b: [0] },
    selectedRows: [],
  });
  assert.deepEqual(result.outputs, []);
  assert.deepEqual(result.errors, []);
  assert.deepEqual(result.results, []);
});

test('malformed expression trees are rejected at compile time', () => {
  assert.throws(() => compile({ kind: 'nope' }), /Unknown expression kind/);
  assert.throws(() => compile({ kind: 'column' }), /non-empty string name/);
  assert.throws(() => compile({ kind: 'compare', op: '??', left: lit(1), right: lit(2) }), /Unsupported comparison/);
  assert.throws(
    () => compile({ kind: 'case', branches: [] }),
    /non-empty branches array/,
  );
  assert.throws(() => compile(null), /Invalid expression node/);
});

test('unknown column inside a selected row is reported as a row-level error', () => {
  const { outputs, errors } = compile(col('missing')).execute({
    columns: { a: [1] },
    selectedRows: [0],
  });
  assert.deepEqual(outputs, []);
  assert.equal(errors[0].row, 0);
  assert.equal(errors[0].code, 'UNKNOWN_COLUMN');
});

test('ExpressionError exposes the input row number for callers catching it directly', () => {
  const err = new ExpressionError('DIVISION_BY_ZERO', 7, 'Division by zero', { numerator: 1 });
  assert.equal(err.row, 7);
  assert.equal(err.code, 'DIVISION_BY_ZERO');
  assert.deepEqual(err.toJSON(), {
    row: 7,
    code: 'DIVISION_BY_ZERO',
    message: 'Division by zero',
    details: { numerator: 1 },
  });
});

test('nested OR three-valued logic with NULLs and short circuit', () => {
  // (a = 1) OR (b = 1) OR (10/c = 1)
  const expr = or(
    or(cmp('eq', col('a'), lit(1)), cmp('eq', col('b'), lit(1))),
    cmp('eq', div(lit(10), col('c')), lit(1)),
  );
  const columns = {
    a: [1, null, null, 0],
    b: [null, 1, null, 0],
    c: [0, 0, 10, 10],
  };
  const { outputs, errors } = compile(expr).execute({
    columns,
    selectedRows: [0, 1, 2, 3],
  });
  // row 0: a=1 -> true, c=0 never reached
  // row 1: b=1 -> true, c=0 never reached
  // row 2: NULL OR NULL -> NULL, then division 10/10=1 -> true
  // row 3: false OR false, then 10/10=1 -> true
  assert.deepEqual(outputs.map((o) => o.value), [true, true, true, true]);
  assert.deepEqual(errors, []);
});
