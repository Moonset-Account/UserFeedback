import test from 'node:test';
import assert from 'node:assert/strict';
import {
  runTransaction,
  getIdentities,
  getIdentityAt,
  IDENTITIES,
} from '../src/index.mjs';

test('array element identities are unique and hidden from enumeration/JSON', () => {
  const doc = { items: [{ v: 'same' }, { v: 'same' }, 'same'] };
  const result = runTransaction(doc, { operations: [] });
  assert.equal(result.ok, true);
  const ids = getIdentities(result.doc, '/items');
  assert.equal(new Set(ids).size, 3);
  assert.equal(Object.keys(result.doc.items).includes('0'), true);
  assert.equal(Object.getOwnPropertySymbols(result.doc.items).includes(IDENTITIES), true);
  const json = JSON.stringify(result.doc);
  assert.equal(json.includes('el_'), false);
  assert.deepEqual(JSON.parse(json), { items: [{ v: 'same' }, { v: 'same' }, 'same'] });
});

test('within one transaction an index remains bound to its start identity after a move', () => {
  const doc = { items: ['same', 'same', 'same'] };
  const beforeIds = getIdentities(doc, '/items');
  const result = runTransaction(doc, {
    operations: [
      { op: 'move', from: '/items/0', count: 1, to: '/items/3' },
      // At this transaction's start /items/2 bound to the third identity.
      // The move changes its physical index to 1, but the write follows identity.
      { op: 'test', path: '/items/2', value: 'same' },
      { op: 'replace', path: '/items/2', value: 'third-identity' },
    ],
  });
  assert.equal(result.ok, true, result.error?.message);
  assert.deepEqual(result.doc.items, ['same', 'third-identity', 'same']);
  const afterIds = getIdentities(result.doc, '/items');
  assert.deepEqual(afterIds, [beforeIds[1], beforeIds[2], beforeIds[0]]);
  assert.equal(result.doc.items[1], 'third-identity');
});

test('in the second transaction the new index binds to the moved identity', () => {
  const doc = { items: ['same', 'same', 'same'] };
  const beforeIds = getIdentities(doc, '/items');
  const first = runTransaction(doc, { operations: [{ op: 'move', from: '/items/0', to: '/items/3' }] });
  assert.equal(first.ok, true);
  const second = runTransaction(first.doc, {
    operations: [
      { op: 'test', path: '/items/2', value: 'same' },
      { op: 'replace', path: '/items/2', value: 'moved-first' },
    ],
  });
  assert.equal(second.ok, true, second.error?.message);
  assert.deepEqual(second.doc.items, ['same', 'same', 'moved-first']);
  assert.equal(getIdentities(second.doc, '/items')[2], beforeIds[0]);
});

test('same-transaction tests read the start snapshot before its writes', () => {
  const doc = { count: 1, items: [{ v: 'old' }] };
  const result = runTransaction(doc, {
    operations: [
      { op: 'test', path: '/count', value: 1 },
      { op: 'replace', path: '/count', value: 2 },
      // This test is compiled against the start snapshot, not the preceding replace.
      { op: 'test', path: '/count', value: 1 },
      { op: 'replace', path: '/items/0/v', value: 'new' },
      { op: 'test', path: '/items/0/v', value: 'old' },
    ],
  });
  assert.equal(result.ok, true, result.error?.message);
  assert.equal(result.doc.count, 2);
  assert.equal(result.doc.items[0].v, 'new');
});

test('two ordinary writes to one hidden identity abort the whole transaction', () => {
  const doc = { items: [{ a: 1, b: 2 }] };
  const jsonBefore = JSON.stringify(doc);
  const result = runTransaction(doc, {
    operations: [
      { op: 'replace', path: '/items/0/a', value: 10 },
      { op: 'replace', path: '/items/0/b', value: 20 },
    ],
  });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, 'WriteConflict');
  assert.equal(JSON.stringify(doc), jsonBefore);
  assert.deepEqual(doc.items[0], { a: 1, b: 2 });
});

test('writes anywhere inside the same bound identity-bearing aggregate conflict', () => {
  const doc = { items: [{ name: 'x', tags: ['a', 'b'] }] };
  const result = runTransaction(doc, {
    operations: [
      { op: 'replace', path: '/items/0/name', value: 'y' },
      { op: 'replace', path: '/items/0/tags/0', value: 'z' },
    ],
  });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, 'WriteConflict');
});

test('writing below a deleted ancestor is a conflict and leaves the document unchanged', () => {
  const doc = { item: { name: 'x', child: { ok: true } }, other: 1 };
  const jsonBefore = JSON.stringify(doc);
  const result = runTransaction(doc, {
    operations: [
      { op: 'remove', path: '/item' },
      { op: 'replace', path: '/item/name', value: 'y' },
      { op: 'replace', path: '/other', value: 2 },
    ],
  });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, 'WriteConflict');
  assert.equal(JSON.stringify(doc), jsonBefore);
  assert.equal(doc.other, 1);
});

test('insertions in the same start gap are committed in operation order', () => {
  const doc = { items: ['a', 'anchor'] };
  const result = runTransaction(doc, {
    operations: [
      { op: 'insert', path: '/items/1', value: 'one' },
      { op: 'insert', path: '/items/1', value: 'two' },
      { op: 'insert', path: '/items/1', value: 'three' },
    ],
  });
  assert.equal(result.ok, true, result.error?.message);
  assert.deepEqual(result.doc.items, ['a', 'one', 'two', 'three', 'anchor']);
});

test('missing and null are distinct test conditions', () => {
  const doc = { nil: null, present: 0 };
  assert.equal(runTransaction(doc, { operations: [{ op: 'test', path: '/nil', value: null }] }).ok, true);
  assert.equal(runTransaction(doc, { operations: [{ op: 'testAbsent', path: '/missing' }] }).ok, true);
  const nullAsMissing = runTransaction(doc, { operations: [{ op: 'testAbsent', path: '/nil' }] });
  assert.equal(nullAsMissing.ok, false);
  assert.equal(nullAsMissing.error.code, 'TestFailed');
  const missingAsNull = runTransaction(doc, { operations: [{ op: 'test', path: '/missing', value: null }] });
  assert.equal(missingAsNull.ok, false);
  assert.equal(missingAsNull.error.code, 'TestFailed');
});

test('array boundary and existence checks reject invalid patches', () => {
  const doc = { items: [1] };
  assert.equal(runTransaction(doc, { operations: [{ op: 'insert', path: '/items/3', value: 2 }] }).ok, false);
  assert.equal(runTransaction(doc, { operations: [{ op: 'replace', path: '/items/5', value: 2 }] }).ok, false);
  assert.equal(runTransaction(doc, { operations: [{ op: 'remove', path: '/missing' }] }).ok, false);
  const end = runTransaction(doc, { operations: [{ op: 'insert', path: '/items/-', value: 2 }] });
  assert.equal(end.ok, true);
  assert.deepEqual(end.doc.items, [1, 2]);
});
