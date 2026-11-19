import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  isCorsSafelistedHeader,
  selectUnsafeHeaders,
} from '../src/cors-safelist.js';

describe('CORS 安全清单判定', () => {
  test('清单内头名且取值形态合法 => 不需要预检', () => {
    assert.equal(isCorsSafelistedHeader('Accept'), true);
    assert.equal(isCorsSafelistedHeader('accept-language'), true);
    assert.equal(isCorsSafelistedHeader('Content-Language'), true);
    assert.equal(isCorsSafelistedHeader('content-type', 'text/plain; charset=utf-8'), true);
    assert.equal(isCorsSafelistedHeader('content-type', 'application/x-www-form-urlencoded'), true);
    assert.equal(isCorsSafelistedHeader('Range', 'bytes=0-1023'), true);
  });

  test('content-type 非清单本质类型 => 需要预检', () => {
    assert.equal(isCorsSafelistedHeader('content-type', 'application/json'), false);
    assert.equal(isCorsSafelistedHeader('Content-Type', 'text/xml'), false);
  });

  test('range 形态非法 => 需要预检', () => {
    assert.equal(isCorsSafelistedHeader('range', 'items=0-10'), false);
    assert.equal(isCorsSafelistedHeader('range', 'bytes=abc-def'), false);
  });

  test('清单外头（含 Authorization）一律需要预检', () => {
    assert.equal(isCorsSafelistedHeader('Authorization', 'Bearer x'), false);
    assert.equal(isCorsSafelistedHeader('x-custom'), false);
    assert.equal(isCorsSafelistedHeader('X-PING', 'pong'), false);
  });

  test('selectUnsafeHeaders 折叠小写、去重并保序', () => {
    const unsafe = selectUnsafeHeaders([
      { name: 'Accept', value: '*/*' },
      { name: 'Content-Type', value: 'application/json' },
      { name: 'X-Custom', value: '1' },
      'authorization',
      'X-CUSTOM', // 重复，丢弃
      { name: 'range', value: 'bytes=0-99' },
    ]);
    assert.deepEqual(unsafe, ['content-type', 'x-custom', 'authorization']);
  });

  test('非法入参抛 TypeError', () => {
    assert.throws(() => isCorsSafelistedHeader(''), TypeError);
    assert.throws(() => selectUnsafeHeaders('nope'), TypeError);
  });
});
