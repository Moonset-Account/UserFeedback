import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AddressSelector } from '../src/address-selector.js';

function drain(selector) {
  const out = [];
  for (;;) {
    const item = selector.next();
    if (item === null) return out;
    out.push(`${item.family}:${item.address}`);
  }
}

test('IPv6 first, then alternates while both families have candidates', () => {
  const selector = new AddressSelector(
    ['v6-1', 'v6-2', 'v6-3'],
    ['v4-1', 'v4-2'],
  );
  assert.deepEqual(drain(selector), [
    'ipv6:v6-1',
    'ipv4:v4-1',
    'ipv6:v6-2',
    'ipv4:v4-2',
    'ipv6:v6-3',
  ]);
});

test('after one family is exhausted, the other is drained in input order', () => {
  const v6First = new AddressSelector(['v6-1'], ['v4-1', 'v4-2', 'v4-3']);
  assert.deepEqual(drain(v6First), [
    'ipv6:v6-1',
    'ipv4:v4-1',
    'ipv4:v4-2',
    'ipv4:v4-3',
  ]);

  const v4Left = new AddressSelector(
    ['v6-1', 'v6-2', 'v6-3', 'v6-4'],
    ['v4-1'],
  );
  assert.deepEqual(drain(v4Left), [
    'ipv6:v6-1',
    'ipv4:v4-1',
    'ipv6:v6-2',
    'ipv6:v6-3',
    'ipv6:v6-4',
  ]);
});

test('single-family inputs keep their original order', () => {
  assert.deepEqual(drain(new AddressSelector(['a', 'b', 'c'], [])), [
    'ipv6:a', 'ipv6:b', 'ipv6:c',
  ]);
  assert.deepEqual(drain(new AddressSelector([], ['x', 'y'])), [
    'ipv4:x', 'ipv4:y',
  ]);
});

test('empty selector reports no next item', () => {
  const selector = new AddressSelector([], []);
  assert.equal(selector.hasNext(), false);
  assert.equal(selector.remaining, 0);
  assert.equal(selector.next(), null);
});

test('hasNext and remaining reflect consumption', () => {
  const selector = new AddressSelector(['a', 'b'], ['x']);
  assert.equal(selector.remaining, 3);
  assert.equal(selector.hasNext(), true);
  selector.next();
  assert.equal(selector.remaining, 2);
  selector.next();
  selector.next();
  assert.equal(selector.remaining, 0);
  assert.equal(selector.hasNext(), false);
});

test('returned entries carry the index inside the family array', () => {
  const selector = new AddressSelector(['a'], ['x', 'y']);
  assert.deepEqual(selector.next(), { address: 'a', family: 'ipv6', index: 0 });
  assert.deepEqual(selector.next(), { address: 'x', family: 'ipv4', index: 0 });
  assert.deepEqual(selector.next(), { address: 'y', family: 'ipv4', index: 1 });
});
