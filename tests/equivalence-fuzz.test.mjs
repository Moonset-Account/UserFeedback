import test from 'node:test';
import assert from 'node:assert/strict';
import { isDeepStrictEqual } from 'node:util';
import { runSequential } from '../src/transaction.mjs';
import { executeComposed } from '../src/compose.mjs';
import { invertPatch, applyPatch } from '../src/apply.mjs';
import { getIdentities } from '../src/document.mjs';

function mulberry32(seed) {
  return function rand() {
    let t = seed += 0x6D2B79F5;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const int = (r, n) => (n <= 0 ? 0 : Math.floor(r() * n));
const pick = (r, arr) => arr[int(r, arr.length)];

function makeDoc(r) {
  return {
    items: Array.from({ length: 2 + int(r, 4) }, () => ({
      v: pick(r, ['same', 'same', 'x', 'y', null]),
      n: int(r, 5),
    })),
    title: pick(r, ['alpha', 'beta', null]),
    nil: null,
  };
}

function randomTx(doc, r) {
  const len = getIdentities(doc, '/items').length;
  const ops = [];
  const count = 1 + int(r, 3);
  for (let k = 0; k < count; k += 1) {
    const kind = len === 0 ? 'insert' : pick(r, ['test', 'test', 'replace', 'remove', 'insert', 'move', 'conflict']);
    const i = int(r, len);
    if (kind === 'test') {
      if (r() < 0.15) ops.push({ op: 'testAbsent', path: `/items/${len + int(r, 3)}` });
      else ops.push({ op: 'test', path: `/items/${i}/v`, value: doc.items[i].v });
    } else if (kind === 'replace') {
      ops.push({ op: 'replace', path: `/items/${i}/v`, value: pick(r, ['same', 'z', 'Q', null]) });
    } else if (kind === 'remove') {
      ops.push({ op: 'remove', path: `/items/${i}` });
    } else if (kind === 'insert') {
      ops.push({ op: 'insert', path: `/items/${int(r, len + 1)}`, value: { v: `new-${k}`, tags: [1, 2] } });
    } else if (kind === 'move' && len > 1) {
      const from = int(r, len);
      const countFrag = 1 + int(r, Math.min(2, len - from));
      ops.push({ op: 'move', from: `/items/${from}`, count: countFrag, to: `/items/${int(r, len + 1)}` });
    } else if (kind === 'conflict' && len > 0) {
      ops.push({ op: 'replace', path: `/items/${i}/v`, value: 'conflict' });
      ops.push({ op: 'remove', path: `/items/${i}` });
    }
  }
  return { operations: ops };
}

test('seeded corpus: sequential and symbolic net composition have equal success sets/results', () => {
  for (let seed = 1; seed <= 500; seed += 1) {
    const r = mulberry32(seed);
    const doc = makeDoc(r);
    const tx1 = randomTx(doc, r);
    const first = runSequential(doc, tx1, { operations: [] });
    const intermediate = first.ok ? first.doc : doc;
    const tx2 = randomTx(intermediate, r);

    const sequential = runSequential(doc, tx1, tx2);
    const composed = executeComposed(doc, tx1, tx2);

    assert.equal(composed.ok, sequential.ok, `seed ${seed}: success mismatch ${composed.error?.code} vs ${sequential.error?.code}`);
    if (sequential.ok) {
      assert.equal(isDeepStrictEqual(composed.doc, sequential.doc), true, `seed ${seed}: document mismatch`);
      assert.ok(composed.composition.netPatch.ops.length > 0 || isDeepStrictEqual(doc, sequential.doc));
    }
  }
});

test('seeded corpus: inverse patches restore values and top-level identity order', () => {
  const r = mulberry32(987654);
  for (let seed = 1; seed <= 300; seed += 1) {
    const doc = makeDoc(r);
    const tx = randomTx(doc, r);
    const run = runTransactionShim(doc, tx);
    if (!run.ok) continue;
    const inverse = invertPatch(doc, run.patch);
    const restored = applyPatch(run.doc, inverse);
    assert.equal(isDeepStrictEqual(restored, doc), true, `inverse value mismatch seed ${seed}`);
    assert.deepEqual(getIdentities(restored, '/items'), getIdentities(doc, '/items'), `identity mismatch seed ${seed}`);
  }
});

function runTransactionShim(doc, tx) {
  // Local import avoids a second top-level binding in the assertion flow.
  return runSequential(doc, tx, { operations: [] }).attempts[0];
}
