import test from 'node:test';
import assert from 'node:assert/strict';
import { isDeepStrictEqual } from 'node:util';
import { compileTransaction } from '../src/compile.mjs';
import { applyPatch, invertPatch } from '../src/apply.mjs';
import { runSequential } from '../src/transaction.mjs';
import { composePatches, planComposition, applyComposedPatch } from '../src/compose.mjs';
import { getIdentities } from '../src/document.mjs';

const moveTx1 = { operations: [{ op: 'move', from: '/items/0', count: 1, to: '/items/3' }] };
const moveTx2 = {
  operations: [
    { op: 'test', path: '/items/2/v', value: 'x' },
    { op: 'replace', path: '/items/2/v', value: 'X' },
  ],
};

test('composes moved-object reference into initial-path tests and index rules', () => {
  const witness = {
    items: [{ v: 'x' }, { v: 'y' }, { v: 'z' }],
    unrelatedMarker: 'DO_NOT_COPY_7319',
  };
  const patch1 = compileTransaction(witness, moveTx1);
  const intermediate = applyPatch(witness, patch1);
  const patch2 = compileTransaction(intermediate, moveTx2);
  const composed = composePatches(patch1, patch2, { base: witness });

  assert.equal(composed.ok, true, composed.error?.message);
  assert.ok(composed.initialTests.some((t) => t.kind === 'value' && t.path === '/items/0/v' && t.expected === 'x'));
  assert.ok(composed.initialTests.some((t) => t.kind === 'arrayLength' && t.path === '/items' && t.length === 3));
  assert.ok(composed.indexRules.some((r) => r.initialPath === '/items/0'));
  assert.ok(composed.netPatch.ops.some((op) => op.kind === 'arrayTransform'));

  const serialized = JSON.stringify(composed);
  assert.equal(serialized.includes('DO_NOT_COPY_7319'), false);
  assert.equal(serialized.includes('el_'), false);
});

test('fixed composed patch has the same success set and values on qualifying documents', () => {
  const witness = { items: [{ v: 'x' }, { v: 'y' }, { v: 'z' }] };
  const composed = planComposition(witness, moveTx1, moveTx2);
  assert.equal(composed.ok, true);

  const candidates = [
    { items: [{ v: 'x' }, { v: 'y' }, { v: 'z' }] },
    { items: [{ v: 'x' }, { v: 'totally-different-y' }, { v: 42 }] },
    { items: [{ v: 'wrong' }, { v: 'y' }, { v: 'z' }] },
    { items: [null, { v: 'y' }, { v: 'z' }] },
  ];

  const portableComposed = JSON.parse(JSON.stringify(composed));
  const portableResult = applyComposedPatch(candidates[0], portableComposed);
  assert.equal(portableResult.ok, true, portableResult.error?.message);

  for (const candidate of candidates) {
    const sequential = runSequential(candidate, moveTx1, moveTx2);
    const net = applyComposedPatch(candidate, composed);
    assert.equal(net.ok, sequential.ok, `success mismatch for ${JSON.stringify(candidate)}: ${net.error?.message}`);
    if (sequential.ok) {
      assert.equal(isDeepStrictEqual(net.doc, sequential.doc), true);
      const initialIds = getIdentities(candidate, '/items');
      assert.equal(getIdentities(net.doc, '/items')[2], initialIds[0]);
    }
  }
});

test('two JSON-identical arrays with different hidden identities move and test the correct identity', () => {
  const first = { items: ['same', 'same', 'same'] };
  const second = { items: ['same', 'same', 'same'] };
  const idA = getIdentities(first, '/items');
  const idB = getIdentities(second, '/items');
  assert.equal(idA.every((id, i) => id !== idB[i]), true);

  const composedA = planComposition(first, moveTx1, {
    operations: [
      { op: 'test', path: '/items/2', value: 'same' },
      { op: 'replace', path: '/items/2', value: 'moved' },
    ],
  });
  const composedB = planComposition(second, moveTx1, {
    operations: [
      { op: 'test', path: '/items/2', value: 'same' },
      { op: 'replace', path: '/items/2', value: 'moved' },
    ],
  });
  assert.equal(composedA.ok && composedB.ok, true);
  const outA = applyComposedPatch(first, composedA);
  const outB = applyComposedPatch(second, composedB);
  assert.deepEqual(outA.doc.items, ['same', 'same', 'moved']);
  assert.deepEqual(outB.doc.items, ['same', 'same', 'moved']);
  assert.equal(getIdentities(outA.doc, '/items')[2], idA[0]);
  assert.equal(getIdentities(outB.doc, '/items')[2], idB[0]);
});

test('second transaction can reference and modify an object inserted by the first', () => {
  const witness = { items: [{ kind: 'old' }] };
  const tx1 = { operations: [{ op: 'insert', path: '/items/1', value: { kind: 'new', n: 1 } }] };
  const tx2 = {
    operations: [
      { op: 'test', path: '/items/1/kind', value: 'new' },
      { op: 'replace', path: '/items/1/n', value: 2 },
    ],
  };
  const composed = planComposition(witness, tx1, tx2);
  assert.equal(composed.ok, true, composed.error?.message);
  assert.ok(composed.derivedTests.some((t) => t.representation === 'created-object-obligation'));
  const transform = composed.netPatch.ops.find((op) => op.kind === 'arrayTransform');
  assert.equal(transform.order[1].kind, 'new');
  assert.deepEqual(transform.order[1].value, { kind: 'new', n: 2 });

  const other = { items: [{ kind: 'another-old', untouched: true }] };
  const applied = applyComposedPatch(other, composed);
  assert.equal(applied.ok, true, applied.error?.message);
  assert.deepEqual(applied.doc.items, [
    { kind: 'another-old', untouched: true },
    { kind: 'new', n: 2 },
  ]);
});

test('second test of a first replacement becomes a static write obligation, not a witness value test', () => {
  const witness = { a: { n: 0, witnessOnly: 'old-shape' } };
  const tx1 = { operations: [{ op: 'replace', path: '/a', value: { n: 2 } }] };
  const tx2 = {
    operations: [
      { op: 'test', path: '/a/n', value: 2 },
      { op: 'replace', path: '/a/n', value: 3 },
    ],
  };
  const composed = planComposition(witness, tx1, tx2);
  assert.equal(composed.ok, true, composed.error?.message);
  assert.equal(composed.initialTests.some((t) => t.path === '/a/n' && t.kind === 'value'), false);
  assert.ok(composed.derivedTests.some((t) => t.representation === 'static-write-obligation'));
  assert.deepEqual(composed.netPatch.ops.find((op) => op.path === '/a').value, { n: 3 });

  for (const other of [{ a: { weird: true } }, { a: 'scalar-old' }, { a: null }]) {
    const seq = runSequential(other, tx1, tx2);
    const net = applyComposedPatch(other, composed);
    assert.equal(net.ok, seq.ok);
    if (seq.ok) assert.deepEqual(net.doc, seq.doc);
  }
  const missingA = applyComposedPatch({}, composed);
  assert.equal(missingA.ok, false);
});

test('composition preserves identity across arrays when a fragment moves between arrays', () => {
  const doc = {
    from: [{ v: 'move-me' }, { v: 'stay' }],
    to: [{ v: 'destination' }],
  };
  const sourceId = getIdentities(doc, '/from')[0];
  const tx1 = { operations: [{ op: 'move', from: '/from/0', to: '/to/1' }] };
  const tx2 = {
    operations: [
      { op: 'test', path: '/to/1/v', value: 'move-me' },
      { op: 'replace', path: '/to/1/v', value: 'moved-and-edited' },
    ],
  };

  const sequential = runSequential(doc, tx1, tx2);
  assert.equal(sequential.ok, true, sequential.error?.message);
  const composed = planComposition(doc, tx1, tx2);
  assert.equal(composed.ok, true, composed.error?.message);
  const net = applyComposedPatch(doc, composed);
  assert.equal(net.ok, true, net.error?.message);
  assert.deepEqual(net.doc, sequential.doc);
  assert.equal(getIdentities(net.doc, '/to')[1], sourceId);
  assert.deepEqual(net.doc.from, [{ v: 'stay' }]);
  assert.equal(net.doc.to[1].v, 'moved-and-edited');

  const inverse = invertPatch(doc, composed.netPatch);
  const restored = applyPatch(net.doc, inverse);
  assert.deepEqual(restored, doc);
  assert.deepEqual(getIdentities(restored, '/from'), getIdentities(doc, '/from'));
  assert.deepEqual(getIdentities(restored, '/to'), getIdentities(doc, '/to'));
});

test('inverse of a composed net patch restores the qualifying initial document', () => {
  const initial = { items: [{ v: 'x' }, { v: 'y' }, { v: 'z' }], nil: null };
  const composed = planComposition(initial, moveTx1, moveTx2);
  const committed = applyComposedPatch(initial, composed);
  assert.equal(committed.ok, true);
  const inverse = invertPatch(initial, composed.netPatch);
  const restored = applyPatch(committed.doc, inverse);
  assert.deepEqual(restored, initial);
  assert.deepEqual(getIdentities(restored, '/items'), getIdentities(initial, '/items'));
});
