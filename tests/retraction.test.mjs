import test from 'node:test';
import assert from 'node:assert/strict';

import { BigramLedger, buildFromScratch, replay } from '../src/bigram-ledger.js';

function counts(ledger) {
  return Object.fromEntries(
    ledger.query().map((candidate) => [candidate.bigram.join(' '), candidate.count]),
  );
}

test('retracting a middle segment drops contributions that include it', () => {
  const ledger = new BigramLedger().ingest([
    { id: 'seg-1', session: 'call-a', tokens: ['alpha', 'beta'] },
    { id: 'seg-2', session: 'call-a', tokens: ['gamma', 'delta'] },
    { id: 'seg-3', session: 'call-a', tokens: ['epsilon', 'zeta'] },
  ]);

  assert.deepEqual(counts(ledger), {
    'alpha beta': 1,
    'beta gamma': 1,
    'gamma delta': 1,
    'delta epsilon': 1,
    'epsilon zeta': 1,
  });

  ledger.markPrivate('seg-2');

  // gamma and delta lived in seg-2, so beta->gamma, gamma->delta and
  // delta->epsilon all go. The outer pairs survive, and beta->epsilon is
  // never invented across the retracted gap.
  assert.deepEqual(counts(ledger), {
    'alpha beta': 1,
    'epsilon zeta': 1,
  });
  assert.equal(ledger.countFor('beta', 'epsilon'), 0);
  assert.equal(ledger.countFor('beta', 'delta'), 0);
});

test('retracting an end segment keeps the remaining internal pairs', () => {
  const ledger = new BigramLedger().ingest([
    { id: 'seg-1', session: 'call-a', tokens: ['alpha', 'beta'] },
    { id: 'seg-2', session: 'call-a', tokens: ['gamma', 'delta'] },
  ]);

  ledger.markPrivate('seg-1');

  // alpha->beta and the cross-boundary beta->gamma both involved seg-1.
  assert.deepEqual(counts(ledger), { 'gamma delta': 1 });
});

test('other public sources keep their contribution to a shared bigram', () => {
  const ledger = new BigramLedger().ingest([
    { id: 'seg-1', session: 'call-a', tokens: ['hello', 'world'] },
    { id: 'seg-2', session: 'call-b', tokens: ['hello', 'world'] },
    { id: 'seg-3', session: 'call-a', tokens: ['alpha', 'hello', 'world'] },
  ]);

  assert.equal(ledger.countFor('hello', 'world'), 3);

  ledger.markPrivate('seg-3');

  // Only seg-3's instance is withdrawn; the two other sources remain.
  assert.equal(ledger.countFor('hello', 'world'), 2);
  assert.deepEqual(counts(ledger), {
    'hello world': 2,
  });
  // alpha->hello was seg-3-only, so it is gone entirely.
  assert.equal(ledger.countFor('alpha', 'hello'), 0);
});

test('double retraction is idempotent', () => {
  const ledger = new BigramLedger().ingest([
    { id: 'seg-1', session: 'call-a', tokens: ['hello', 'world'] },
    { id: 'seg-2', session: 'call-b', tokens: ['hello', 'world'] },
  ]);

  ledger.markPrivate('seg-1');
  const afterFirst = counts(ledger);
  ledger.markPrivate('seg-1');

  assert.deepEqual(counts(ledger), afterFirst);
  assert.equal(ledger.countFor('hello', 'world'), 1);
  assert.equal(ledger.isPrivate('seg-1'), true);
});

test('retraction blocks pairs appended before and after the retraction', () => {
  const ledger = new BigramLedger();
  ledger.defineSegment({ id: 'seg-1', session: 'call-a' });
  ledger.defineSegment({ id: 'seg-2', session: 'call-a' });
  ledger.defineSegment({ id: 'seg-3', session: 'call-a' });

  ledger.append([
    { text: 'x', segment: 'seg-1' },
    { text: 'y', segment: 'seg-2' },
  ]);
  ledger.markPrivate('seg-2');
  // A later public token appended next to the retracted seg-2 token must not
  // form a pair with it.
  ledger.append([
    { text: 'z', segment: 'seg-2' },
    { text: 'w', segment: 'seg-3' },
  ]);

  assert.deepEqual(counts(ledger), {});
});

test('retracting every segment leaves no candidates', () => {
  const ledger = new BigramLedger().ingest([
    { id: 'seg-1', session: 'call-a', tokens: ['alpha', 'beta'] },
    { id: 'seg-2', session: 'call-a', tokens: ['gamma'] },
  ]);

  ledger.markPrivate('seg-1');
  ledger.markPrivate('seg-2');

  assert.deepEqual(ledger.query(), []);
  assert.equal(ledger.countFor('alpha', 'beta'), 0);
});

test('replay of the retraction log matches a fresh build from final flags', () => {
  const transcript = {
    segments: [
      { id: 'seg-1', session: 'call-a' },
      { id: 'seg-2', session: 'call-a' },
      { id: 'seg-3', session: 'call-b' },
      { id: 'seg-4', session: 'call-b' },
      { id: 'seg-5', session: 'call-b' },
    ],
    tokens: [
      { text: 'alpha', segment: 'seg-1' },
      { text: 'beta', segment: 'seg-1' },
      { text: 'gamma', segment: 'seg-2' },
      { text: 'delta', segment: 'seg-3' },
      { text: 'epsilon', segment: 'seg-3' },
      { text: 'zeta', segment: 'seg-4' },
      { text: 'eta', segment: 'seg-5' },
    ],
  };

  const live = new BigramLedger().ingestTranscript(transcript);
  live.markPrivate('seg-2');
  live.markPrivate('seg-4');

  const replayed = replay(live.events());
  const scratch = buildFromScratch(transcript, { privateSegmentIds: ['seg-2', 'seg-4'] });

  assert.deepEqual(replayed.query(), live.query());
  assert.deepEqual(replayed.query(), scratch.query());
  assert.deepEqual(counts(replayed), counts(scratch));
});

test('replay matches scratch for a segment private from the start', () => {
  const transcript = {
    segments: [
      { id: 'seg-1', session: 'call-a', private: true },
      { id: 'seg-2', session: 'call-a' },
      { id: 'seg-3', session: 'call-a' },
    ],
    tokens: [
      { text: 'alpha', segment: 'seg-1' },
      { text: 'beta', segment: 'seg-2' },
      { text: 'gamma', segment: 'seg-3' },
    ],
  };

  const live = new BigramLedger().ingestTranscript(transcript);
  live.markPrivate('seg-2');

  const replayed = replay(live.events());
  const scratch = buildFromScratch(transcript, { privateSegmentIds: ['seg-2'] });

  assert.deepEqual(replayed.query(), scratch.query());
  // Every pair touches a private segment, so nothing survives either path.
  assert.deepEqual(replayed.query(), []);
});

test('replay rejects an unknown event type', () => {
  assert.throws(() => replay([{ type: 'bogus' }]), /unknown event type/);
});
