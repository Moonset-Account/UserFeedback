import test from 'node:test';
import assert from 'node:assert/strict';
import { isDeepStrictEqual } from 'node:util';
import { runTransaction } from '../src/transaction.mjs';
import { invertPatch, applyPatch } from '../src/apply.mjs';
import { getIdentities } from '../src/document.mjs';

function identityMap(root, path = '', out = {}) {
  if (Array.isArray(root)) {
    out[path || '/'] = getIdentities(root);
    root.forEach((child, i) => identityMap(child, `${path}/${i}`, out));
  } else if (root && typeof root === 'object') {
    for (const [key, value] of Object.entries(root)) identityMap(value, `${path}/${key}`, out);
  }
  return out;
}

function undo(before, tx) {
  const run = runTransaction(before, tx);
  assert.equal(run.ok, true, run.error?.message);
  const inverse = invertPatch(before, run.patch);
  const restored = applyPatch(run.doc, inverse);
  return { run, inverse, restored };
}

test('inverse restores removed array element identity, value, and order', () => {
  const before = { items: [{ k: 'a' }, { k: 'b' }, { k: 'c' }] };
  const beforeIds = getIdentities(before, '/items');
  const { restored } = undo(before, { operations: [{ op: 'remove', path: '/items/1' }] });
  assert.deepEqual(restored, before);
  assert.deepEqual(getIdentities(restored, '/items'), beforeIds);
});

test('inverse restores moved fragments and every nested hidden identity', () => {
  const before = {
    groups: [
      { name: 'a', tags: ['x', 'y'] },
      { name: 'b', tags: ['p'] },
      { name: 'c', tags: ['q', 'r'] },
      { name: 'd', tags: [] },
    ],
  };
  const beforeIds = identityMap(before);
  const { restored } = undo(before, {
    operations: [{ op: 'move', from: '/groups/0', count: 2, to: '/groups/4' }],
  });
  assert.equal(isDeepStrictEqual(restored, before), true);
  assert.deepEqual(identityMap(restored), beforeIds);
});

test('inverse restores a missing object key and an explicit null differently', () => {
  const before = { present: 'value', nil: null };
  const { restored: afterInsertUndo } = undo(before, {
    operations: [{ op: 'insert', path: '/added', value: { hello: 'world' } }],
  });
  assert.equal(Object.hasOwn(afterInsertUndo, 'added'), false);
  assert.deepEqual(afterInsertUndo, before);

  const { run, restored } = undo(before, {
    operations: [{ op: 'replace', path: '/present', value: null }],
  });
  assert.equal(run.doc.present, null);
  assert.equal('present' in restored, true);
  assert.equal(restored.present, 'value');
  assert.equal(restored.nil, null);
  assert.deepEqual(restored, before);
});

test('inverse patch survives JSON round-trip while restoring nested identities', () => {
  const before = {
    items: [
      { id: 'a', tags: ['x', 'y'] },
      { id: 'b', tags: ['p'] },
    ],
    nil: null,
  };
  const run = runTransaction(before, {
    operations: [
      { op: 'remove', path: '/items/1' },
      { op: 'replace', path: '/items/0/tags/0', value: 'changed' },
    ],
  });
  assert.equal(run.ok, true, run.error?.message);
  const inverse = invertPatch(before, run.patch);
  const jsonInverse = JSON.parse(JSON.stringify(inverse));
  const restored = applyPatch(run.doc, jsonInverse);
  assert.deepEqual(restored, before);
  assert.deepEqual(identityMap(restored), identityMap(before));
});

test('inverse restores nested replacements and deletions without losing null', () => {
  const before = {
    root: {
      arr: [
        { keep: 1, child: { value: 'old', empty: null } },
        { keep: 2 },
      ],
    },
  };
  const beforeIds = identityMap(before);
  const { restored } = undo(before, {
    operations: [
      { op: 'replace', path: '/root/arr/0/child/value', value: 'new' },
      { op: 'remove', path: '/root/arr/1' },
    ],
  });
  assert.deepEqual(restored, before);
  assert.deepEqual(identityMap(restored), beforeIds);
});
