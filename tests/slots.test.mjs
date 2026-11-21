import { test } from 'node:test';
import assert from 'node:assert/strict';

import { SlotPool } from '../src/slots.mjs';
import { SlotExhaustedError } from '../src/errors.mjs';
import { requiredBlockIds, requiredSlotCount } from '../src/manifest.mjs';

test('reserve is atomic: a reservation that cannot fit changes nothing', () => {
  const pool = new SlotPool(4);
  pool.reserve('a', 3);
  assert.throws(() => pool.reserve('b', 2), SlotExhaustedError);
  assert.equal(pool.used, 3);
  assert.equal(pool.free, 1);
  assert.equal(pool.isReserved('b'), false);
});

test('reserve reports the shortfall', () => {
  const pool = new SlotPool(2);
  pool.reserve('a', 1);
  try {
    pool.reserve('b', 2);
    assert.fail('expected SlotExhaustedError');
  } catch (err) {
    assert.equal(err.name, 'SlotExhaustedError');
    assert.equal(err.needed, 2);
    assert.equal(err.free, 1);
  }
});

test('release and transfer account for every slot', () => {
  const pool = new SlotPool(5);
  pool.reserve('load', 3);
  pool.transfer('load', 'published');
  assert.equal(pool.free, 2);
  assert.equal(pool.isReserved('published'), true);
  assert.equal(pool.release('published'), 3);
  assert.equal(pool.free, 5);
  assert.equal(pool.release('published'), 0);
});

test('transfer between unknown owners is refused', () => {
  const pool = new SlotPool(3);
  assert.throws(() => pool.transfer('ghost', 'x'), /no reservation/);
  pool.reserve('a', 1);
  pool.reserve('b', 1);
  assert.throws(() => pool.transfer('a', 'b'), /already reserved/);
  assert.equal(pool.used, 2);
});

test('double reservation for the same owner is refused', () => {
  const pool = new SlotPool(3);
  pool.reserve('a', 1);
  assert.throws(() => pool.reserve('a', 1), /already reserved/);
  assert.equal(pool.used, 1);
});

test('invalid capacity and counts are rejected', () => {
  assert.throws(() => new SlotPool(0), RangeError);
  assert.throws(() => new SlotPool(1.5), RangeError);
  const pool = new SlotPool(2);
  assert.throws(() => pool.reserve('a', 0), RangeError);
  assert.throws(() => pool.reserve('a', -1), RangeError);
});

test('requiredBlockIds sorts and de-duplicates nothing (duplicates are an error)', () => {
  assert.deepEqual(requiredBlockIds({ version: 1, blocks: [5, 1, 3], slotCount: 3 }), [1, 3, 5]);
  assert.throws(
    () => requiredBlockIds({ version: 1, blocks: [1, 1], slotCount: 2 }),
    /duplicate block/,
  );
  assert.throws(() => requiredBlockIds({ version: 1, blocks: [], slotCount: 1 }), TypeError);
});

test('requiredSlotCount honours a declared count that exceeds the block count', () => {
  assert.equal(requiredSlotCount({ version: 2, blocks: [0, 1], slotCount: 4 }), 4);
});

test('requiredSlotCount refuses a declared count below the block count', () => {
  assert.throws(
    () => requiredSlotCount({ version: 2, blocks: [0, 1, 2], slotCount: 2 }),
    /declares 2 slots for 3 blocks/,
  );
});

test('manifest validation rejects malformed declarations', () => {
  assert.throws(() => requiredBlockIds(null), TypeError);
  assert.throws(() => requiredBlockIds({ version: -1, blocks: [0], slotCount: 1 }), TypeError);
  assert.throws(() => requiredBlockIds({ version: 1, blocks: [-1], slotCount: 1 }), TypeError);
  assert.throws(() => requiredBlockIds({ version: 1, blocks: ['0'], slotCount: 1 }), TypeError);
  assert.throws(
    () => requiredBlockIds({ version: 1, blocks: [0], slotCount: 0 }),
    TypeError,
  );
});
