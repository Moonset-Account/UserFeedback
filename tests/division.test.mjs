import { test } from 'node:test';
import assert from 'node:assert/strict';
import { compile, truncateTowardZero } from '../src/index.js';

const col = (name) => ({ kind: 'column', name });
const div = (left, right) => ({ kind: 'div', left, right });

function run(expr, columns, selectedRows, validity) {
  return compile(expr).execute({ columns, selectedRows, validity });
}

test('integer division truncates toward zero for positive and negative operands', () => {
  // 7/2=3, -7/2=-3 (toward zero, not floor -4), 7/-2=-3, -7/-2=3, 0/5=0
  const columns = {
    a: [7, -7, 7, -7, 0],
    b: [2, 2, -2, -2, 5],
  };
  const { outputs, errors } = run(div(col('a'), col('b')), columns, [0, 1, 2, 3, 4]);
  assert.deepEqual(outputs.map((o) => o.value), [3, -3, -3, 3, 0]);
  assert.deepEqual(errors, []);
});

test('truncateTowardZero helper behaves correctly at the sign boundary', () => {
  assert.equal(truncateTowardZero(0.9), 0);
  assert.equal(truncateTowardZero(-0.9), 0);
  assert.equal(truncateTowardZero(-3.5), -3);
  assert.equal(truncateTowardZero(3.5), 3);
});

test('NULL numerator yields NULL and denominator is never evaluated (no phantom div-by-zero)', () => {
  const columns = { a: [null, 1], b: [0, 2] };
  const { outputs, errors } = run(div(col('a'), col('b')), columns, [0, 1]);
  assert.deepEqual(outputs, [
    { row: 0, value: null },
    { row: 1, value: 0 },
  ]);
  assert.deepEqual(errors, []);
});

test('NULL denominator yields NULL (including non-null numerator)', () => {
  const columns = { a: [9, null], b: [null, null] };
  const { outputs, errors } = run(div(col('a'), col('b')), columns, [0, 1]);
  assert.deepEqual(outputs.map((o) => o.value), [null, null]);
  assert.deepEqual(errors, []);
});

test('non-null numerator with zero denominator is rejected and reports the input row number', () => {
  const columns = { a: [10, 11, 12, 13], b: [2, 0, 0, 4] };
  const { outputs, errors, results } = run(div(col('a'), col('b')), columns, [0, 1, 2, 3]);

  assert.deepEqual(outputs, [
    { row: 0, value: 5 },
    { row: 3, value: 3 },
  ]);
  assert.deepEqual(errors, [
    {
      row: 1,
      code: 'DIVISION_BY_ZERO',
      message: 'Division by zero',
      details: { numerator: 11 },
    },
    {
      row: 2,
      code: 'DIVISION_BY_ZERO',
      message: 'Division by zero',
      details: { numerator: 12 },
    },
  ]);
  // The interleaved view keeps selection order and marks each status.
  assert.deepEqual(
    results.map((r) => ({ row: r.row, status: r.status })),
    [
      { row: 0, status: 'value' },
      { row: 1, status: 'error' },
      { row: 2, status: 'error' },
      { row: 3, status: 'value' },
    ],
  );
});

test('div-by-zero error carries the ORIGINAL input row number under reordered selection', () => {
  const columns = { a: [1, 2, 3], b: [0, 5, 0] };
  const { errors } = run(div(col('a'), col('b')), columns, [2, 1, 0]);
  assert.deepEqual(errors.map((e) => e.row), [2, 0]);
  assert.ok(errors.every((e) => e.code === 'DIVISION_BY_ZERO'));
});

test('validity bitmap forces nullness: invalid numerator is NULL even with a zero denominator', () => {
  const columns = { a: [8, 8], b: [0, 0] };
  const validity = { a: [0, 1] }; // row 0 invalid -> NULL; row 1 valid -> div by zero
  const { outputs, errors } = run(div(col('a'), col('b')), columns, [0, 1], validity);
  assert.deepEqual(outputs, [{ row: 0, value: null }]);
  assert.equal(errors.length, 1);
  assert.equal(errors[0].row, 1);
  assert.equal(errors[0].code, 'DIVISION_BY_ZERO');
});

test('validity bitmap on denominator: invalid denominator acts as SQL NULL', () => {
  const columns = { a: [8, 8], b: [0, 0] };
  const validity = { b: [0, 1] }; // row 0 invalid -> NULL; row 1 valid zero -> error
  const { outputs, errors } = run(div(col('a'), col('b')), columns, [0, 1], validity);
  assert.deepEqual(outputs, [{ row: 0, value: null }]);
  assert.equal(errors.length, 1);
  assert.equal(errors[0].row, 1);
  assert.equal(errors[0].code, 'DIVISION_BY_ZERO');
});
