import test from 'node:test';
import assert from 'node:assert/strict';

import { BigramLedger, buildFromScratch, replay } from '../src/bigram-ledger.js';

function counts(ledger) {
  return Object.fromEntries(
    ledger.query().map((candidate) => [candidate.bigram.join(' '), candidate.count]),
  );
}

test('cross-segment bigrams are withdrawn with either retracted endpoint', () => {
  const ledger = new BigramLedger().ingest([
    { id: 'seg-a', session: 'call-a', tokens: ['one', 'two'] },
    { id: 'seg-b', session: 'call-a', tokens: ['three', 'four'] },
  ]);

  assert.equal(ledger.countFor('two', 'three'), 1, 'boundary pair counts while public');

  ledger.markPrivate('seg-b');

  // Both the cross-boundary two->three and the internal three->four were
  // anchored in seg-b, so both go. one->two is untouched.
  assert.deepEqual(counts(ledger), { 'one two': 1 });
});

test('a retracted middle segment never lets its neighbours pair up', () => {
  // This is the interleaved case: the middle segment's tokens are removed from
  // the accounting but remain in the stream, so left and right keep the gap.
  const ledger = new BigramLedger().ingest([
    { id: 'seg-left', session: 'call-a', tokens: ['left'] },
    { id: 'seg-mid', session: 'call-a', tokens: ['mid-1', 'mid-2'] },
    { id: 'seg-right', session: 'call-a', tokens: ['right'] },
  ]);

  assert.deepEqual(counts(ledger), {
    'left mid-1': 1,
    'mid-1 mid-2': 1,
    'mid-2 right': 1,
  });

  ledger.markPrivate('seg-mid');

  assert.deepEqual(counts(ledger), {});
  assert.equal(ledger.countFor('left', 'right'), 0);
  assert.equal(ledger.countFor('left', 'mid-1'), 0);
  assert.equal(ledger.countFor('mid-2', 'right'), 0);
});

test('session break blocks a pair even when both segments stay public', () => {
  const ledger = new BigramLedger().ingest([
    { id: 'seg-a', session: 'call-a', tokens: ['one', 'two'] },
    { id: 'seg-b', session: 'call-b', tokens: ['three', 'four'] },
    { id: 'seg-c', session: 'call-b', tokens: ['five'] },
  ]);

  assert.deepEqual(counts(ledger), {
    'one two': 1,
    'three four': 1,
    'four five': 1,
  });

  // Retracting seg-b removes three->four and four->five but cannot make
  // two->three suddenly legal or two->five appear.
  ledger.markPrivate('seg-b');

  assert.deepEqual(counts(ledger), { 'one two': 1 });
  assert.equal(ledger.countFor('two', 'three'), 0);
  assert.equal(ledger.countFor('two', 'five'), 0);
});

test('shared bigram across sessions survives when one side is retracted', () => {
  const ledger = new BigramLedger().ingest([
    { id: 'seg-a1', session: 'call-a', tokens: ['deep', 'work'] },
    { id: 'seg-a2', session: 'call-a', tokens: ['deep', 'work'] },
    { id: 'seg-b1', session: 'call-b', tokens: ['deep', 'work'] },
  ]);

  assert.equal(ledger.countFor('deep', 'work'), 3);

  ledger.markPrivate('seg-a1');
  ledger.markPrivate('seg-a2');

  // The independent call-b contribution is preserved.
  assert.equal(ledger.countFor('deep', 'work'), 1);
});

test('interleaved append, retract, append, retract converges to scratch build', () => {
  const transcript = {
    segments: [
      { id: 's1', session: 'call-a' },
      { id: 's2', session: 'call-a' },
      { id: 's3', session: 'call-a' },
      { id: 's4', session: 'call-b' },
      { id: 's5', session: 'call-b' },
    ],
    tokens: [
      { text: 'a', segment: 's1' },
      { text: 'b', segment: 's2' },
      { text: 'c', segment: 's2' },
      { text: 'd', segment: 's3' },
      { text: 'e', segment: 's4' },
      { text: 'f', segment: 's5' },
      { text: 'g', segment: 's5' },
    ],
  };

  const live = new BigramLedger();
  live.defineSegment({ id: 's1', session: 'call-a' });
  live.defineSegment({ id: 's2', session: 'call-a' });
  live.defineSegment({ id: 's3', session: 'call-a' });
  live.defineSegment({ id: 's4', session: 'call-b' });
  live.defineSegment({ id: 's5', session: 'call-b' });

  live.append(transcript.tokens.slice(0, 3));
  live.markPrivate('s2');
  live.append(transcript.tokens.slice(3));

  // s2 held b and c; every pair touching either is withdrawn. d->e is
  // cross-session (s3 call-a -> s4 call-b) and was blocked from the start.
  assert.equal(live.countFor('e', 'f'), 1);
  assert.equal(live.countFor('d', 'e'), 0);
  assert.equal(live.countFor('a', 'b'), 0);
  assert.equal(live.countFor('c', 'd'), 0);
  assert.equal(live.countFor('b', 'c'), 0);
  assert.deepEqual(counts(live), { 'e f': 1, 'f g': 1 });

  const scratch = buildFromScratch(transcript, { privateSegmentIds: ['s2'] });
  assert.deepEqual(scratch.query(), live.query());
  assert.deepEqual(replay(live.events()).query(), live.query());
});
