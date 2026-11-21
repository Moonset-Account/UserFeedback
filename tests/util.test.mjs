import test from 'node:test';
import assert from 'node:assert/strict';

import {
  compareExperimentIds,
  retentionCount,
  ratioString,
  aggregateFolds,
  CoordinatorError,
} from '../src/index.js';

test('retentionCount is floor(n/2) with a floor of one', () => {
  assert.equal(retentionCount(0), 0);
  assert.equal(retentionCount(1), 1);
  assert.equal(retentionCount(2), 1);
  assert.equal(retentionCount(3), 1);
  assert.equal(retentionCount(4), 2);
  assert.equal(retentionCount(5), 2);
  assert.equal(retentionCount(7), 3);
  assert.equal(retentionCount(8), 4);
});

test('retentionCount rejects non-integers', () => {
  assert.throws(() => retentionCount(-1), TypeError);
  assert.throws(() => retentionCount(1.5), TypeError);
  assert.throws(() => retentionCount('4'), TypeError);
});

test('experiment ids sort numerically first, then by code unit', () => {
  const ids = ['10', '2', 'alpha', '1', 'beta', '02'];
  const sorted = [...ids].sort(compareExperimentIds);
  assert.deepEqual(sorted, ['1', '02', '2', '10', 'alpha', 'beta']);
  assert.equal(compareExperimentIds('2', '10'), -1);
  assert.equal(compareExperimentIds('x', 'x'), 0);
  // Numerically equal numeric strings fall back to code-unit order.
  assert.equal(compareExperimentIds('01', '1'), -1);
  assert.equal(compareExperimentIds('1', '01'), 1);
});

test('ratioString reduces exactly without floating point', () => {
  assert.equal(ratioString(500, 1000), '1/2');
  assert.equal(ratioString(3, 4), '3/4');
  assert.equal(ratioString(6, 8), '3/4');
  assert.equal(ratioString(0, 7), '0/1');
  assert.equal(ratioString(7, 7), '1/1');
  assert.equal(ratioString(900, 1100), '9/11');
});

test('aggregateFolds sums correct and samples instead of averaging accuracies', () => {
  const result = aggregateFolds([
    { name: 'small', correct: 90, samples: 100 },
    { name: 'large', correct: 800, samples: 1000 },
  ]);
  assert.equal(result.correct, 890);
  assert.equal(result.samples, 1100);
  assert.equal(result.scoreRatio, '89/110');
  // A macro average would be (0.9 + 0.8) / 2 = 0.85; the weighted score is
  // lower because the larger fold scores worse. Confirm we are not macro.
  assert.ok(Math.abs(result.score - 0.85) > 1e-9);
  assert.ok(Math.abs(result.score - 890 / 1100) < 1e-12);
});

test('aggregateFolds rejects malformed counts', () => {
  assert.throws(
    () => aggregateFolds([{ name: 'a', correct: 5, samples: 0 }]),
    (error) => error instanceof CoordinatorError && error.code === 'INVALID_COUNT',
  );
  assert.throws(
    () => aggregateFolds([{ name: 'a', correct: 6, samples: 5 }]),
    (error) => error instanceof CoordinatorError && error.code === 'INVALID_COUNT',
  );
  assert.throws(
    () => aggregateFolds([]),
    (error) => error instanceof CoordinatorError && error.code === 'INVALID_COUNT',
  );
});
