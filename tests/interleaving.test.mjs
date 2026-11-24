import test from 'node:test';
import assert from 'node:assert/strict';
import { runTransaction } from '../src/transaction.mjs';
import { runSequential } from '../src/transaction.mjs';
import { getIdentities } from '../src/document.mjs';

test('move followed by insertion in the same start gap preserves operation order', () => {
  const doc = { items: ['a', 'b', 'c', 'd'] };
  const ids = getIdentities(doc, '/items');
  const result = runTransaction(doc, {
    operations: [
      { op: 'move', from: '/items/0', to: '/items/2' },
      { op: 'insert', path: '/items/2', value: 'I' },
    ],
  });
  assert.equal(result.ok, true, result.error?.message);
  assert.deepEqual(result.doc.items, ['b', 'a', 'I', 'c', 'd']);
  assert.deepEqual(getIdentities(result.doc, '/items'), [ids[1], ids[0], result.doc.items[2] && getIdentities(result.doc, '/items')[2], ids[2], ids[3]]);
});

test('insertion followed by move into the same start gap preserves operation order', () => {
  const doc = { items: ['a', 'b', 'c', 'd'] };
  const ids = getIdentities(doc, '/items');
  const result = runTransaction(doc, {
    operations: [
      { op: 'insert', path: '/items/2', value: 'I' },
      { op: 'move', from: '/items/0', to: '/items/2' },
    ],
  });
  assert.equal(result.ok, true, result.error?.message);
  assert.deepEqual(result.doc.items, ['b', 'I', 'a', 'c', 'd']);
  const afterIds = getIdentities(result.doc, '/items');
  assert.equal(afterIds[2], ids[0]);
  assert.equal(afterIds[0], ids[1]);
  assert.equal(afterIds[3], ids[2]);
  assert.equal(afterIds[4], ids[3]);
});

test('a second transaction binds its own gap indices after the first commit', () => {
  const doc = { items: ['a', 'b', 'c'] };
  const result = runSequential(
    doc,
    { operations: [{ op: 'insert', path: '/items/1', value: 'first' }] },
    { operations: [{ op: 'insert', path: '/items/1', value: 'second-before-first' }] },
  );
  assert.equal(result.ok, true, result.error?.message);
  assert.deepEqual(result.doc.items, ['a', 'second-before-first', 'first', 'b', 'c']);
});
