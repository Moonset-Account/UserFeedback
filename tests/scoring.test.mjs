import { test } from 'node:test';
import assert from 'node:assert/strict';

import { dot, scoreVersion } from '../src/scoring.mjs';

test('dot multiplies and sums integer weights against the input', () => {
  assert.equal(dot([1, 2, 3], [4, 5, 6]), 32);
  assert.equal(dot([-2, 0, 7], [3, -1, 2]), 8);
  assert.equal(dot([], []), 0);
});

test('dot rejects dimension mismatch', () => {
  assert.throws(() => dot([1, 2], [1]), RangeError);
});

test('dot accepts non-integer inputs but integer weights', () => {
  assert.equal(dot([2], [0.5]), 1);
});

test('scoreVersion walks blocks in the given order and keeps a breakdown', () => {
  const blocks = new Map([
    [0, [1, 0]],
    [2, [0, 3]],
  ]);
  const { score, blocks: breakdown } = scoreVersion(blocks, [0, 2], [5, 7]);
  assert.equal(score, 5 + 21);
  assert.deepEqual(
    breakdown.map((b) => [b.blockId, b.contribution]),
    [
      [0, 5],
      [2, 21],
    ],
  );
});

test('scoreVersion rejects missing blocks and non-finite input', () => {
  const blocks = new Map([[0, [1]]]);
  assert.throws(() => scoreVersion(blocks, [0, 1], [1]), /missing weights/);
  assert.throws(() => scoreVersion(blocks, [0], [NaN]), TypeError);
  assert.throws(() => scoreVersion(blocks, [0], [Infinity]), TypeError);
  assert.throws(() => scoreVersion(blocks, [0], ['1']), TypeError);
});
