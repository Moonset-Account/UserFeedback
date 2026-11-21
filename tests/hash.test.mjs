import { test } from 'node:test';
import assert from 'node:assert/strict';
import { keySlot, keysForSlot, findKeyForSlot, SLOT_COUNT } from '../src/hash.mjs';

test('keySlot: 确定性、范围合法、同键同槽', () => {
  const slot1 = keySlot('user:1');
  const slot2 = keySlot('user:1');
  assert.equal(slot1, slot2);
  assert.ok(Number.isInteger(slot1));
  assert.ok(slot1 >= 0 && slot1 < SLOT_COUNT);
});

test('keySlot: 不同键会分散到多个槽（不是恒定值）', () => {
  const seen = new Set();
  for (let i = 0; i < 200; i += 1) seen.add(keySlot(`key:${i}`));
  assert.ok(seen.size > 50, `200 个键只散到 ${seen.size} 个槽，哈希分布可疑`);
});

test('keySlot: 拒绝空键与非字符串', () => {
  assert.throws(() => keySlot(''), TypeError);
  assert.throws(() => keySlot(42), TypeError);
});

test('findKeyForSlot: 生成的键必定落在目标槽', () => {
  for (const slot of [0, 1, 42, 999, 16383]) {
    const key = findKeyForSlot(slot);
    assert.equal(keySlot(key), slot);
  }
});

test('keysForSlot: 生成一组互不相同的同槽键', () => {
  const slot = 700;
  const keys = keysForSlot(slot, 5);
  assert.equal(keys.length, 5);
  assert.equal(new Set(keys).size, 5);
  for (const k of keys) assert.equal(keySlot(k), slot);
});

test('findKeyForSlot: 非法槽号抛 RangeError', () => {
  assert.throws(() => findKeyForSlot(-1), RangeError);
  assert.throws(() => findKeyForSlot(SLOT_COUNT), RangeError);
});
