import test from 'node:test';
import assert from 'node:assert/strict';

import { BigramLedger } from '../src/bigram-ledger.js';

/** Collapse a ledger query into a readable "prev next" -> count map. */
function counts(ledger) {
  return Object.fromEntries(
    ledger.query().map((candidate) => [candidate.bigram.join(' '), candidate.count]),
  );
}

/** Build a ledger with one contiguous block per segment, in the given order. */
function ledgerFrom(segments) {
  return new BigramLedger().ingest(segments);
}

test('adjacent public tokens in the same session add one count each', () => {
  const ledger = ledgerFrom([
    { id: 'seg-1', session: 'call-a', tokens: ['alpha', 'beta', 'gamma'] },
  ]);

  assert.deepEqual(counts(ledger), { 'alpha beta': 1, 'beta gamma': 1 });
  assert.equal(ledger.countFor('alpha', 'beta'), 1);
  assert.equal(ledger.countFor('beta', 'alpha'), 0);
});

test('a repeated bigram accumulates one count per adjacency', () => {
  const ledger = ledgerFrom([
    { id: 'seg-1', session: 'call-a', tokens: ['ping', 'pong', 'ping', 'pong'] },
  ]);

  assert.equal(ledger.countFor('ping', 'pong'), 2);
  assert.equal(ledger.countFor('pong', 'ping'), 1);
});

test('segment boundaries do not break adjacency within a session', () => {
  const ledger = ledgerFrom([
    { id: 'seg-1', session: 'call-a', tokens: ['alpha', 'beta'] },
    { id: 'seg-2', session: 'call-a', tokens: ['gamma', 'delta'] },
  ]);

  // alpha->beta and gamma->delta are intra-segment; beta->gamma crosses the
  // boundary but both sides are public and share a session, so it counts.
  assert.deepEqual(counts(ledger), {
    'alpha beta': 1,
    'beta gamma': 1,
    'gamma delta': 1,
  });
});

test('cross-session adjacency is blocked and contributes nothing', () => {
  const ledger = ledgerFrom([
    { id: 'seg-1', session: 'call-a', tokens: ['alpha', 'beta'] },
    { id: 'seg-2', session: 'call-b', tokens: ['gamma', 'delta'] },
  ]);

  assert.deepEqual(counts(ledger), {
    'alpha beta': 1,
    'gamma delta': 1,
  });
  assert.equal(ledger.countFor('beta', 'gamma'), 0);
});

test('a segment private at creation is a barrier, not a filter', () => {
  const ledger = ledgerFrom([
    { id: 'seg-1', session: 'call-a', tokens: ['alpha'] },
    { id: 'seg-2', session: 'call-a', private: true, tokens: ['secret'] },
    { id: 'seg-3', session: 'call-a', tokens: ['omega'] },
  ]);

  // Neither alpha->secret nor secret->omega counts, and alpha->omega must not
  // be invented now that the middle tokens are withheld.
  assert.deepEqual(counts(ledger), {});
  assert.equal(ledger.countFor('alpha', 'omega'), 0);
});

test('privately flagged tokens never seed a pair with the stream tail', () => {
  const ledger = new BigramLedger();
  ledger.defineSegment({ id: 'seg-1', session: 'call-a' });
  ledger.defineSegment({ id: 'seg-2', session: 'call-a', private: true });
  ledger.defineSegment({ id: 'seg-3', session: 'call-a' });

  ledger.append([{ text: 'alpha', segment: 'seg-1' }]);
  ledger.append([{ text: 'secret', segment: 'seg-2' }]);
  ledger.append([{ text: 'omega', segment: 'seg-3' }]);

  assert.deepEqual(counts(ledger), {});
  assert.equal(ledger.countFor('alpha', 'secret'), 0);
  assert.equal(ledger.countFor('secret', 'omega'), 0);
  assert.equal(ledger.countFor('alpha', 'omega'), 0);
});

test('query candidates are ordered by token pair and stable', () => {
  const ledger = ledgerFrom([
    { id: 'seg-1', session: 'call-a', tokens: ['b', 'a', 'c', 'a'] },
  ]);

  const ordered = ledger.query().map((candidate) => candidate.bigram);
  assert.deepEqual(ordered, [
    ['a', 'c'],
    ['b', 'a'],
    ['c', 'a'],
  ]);
});

test('an appended token must reference a registered segment', () => {
  const ledger = new BigramLedger();
  ledger.defineSegment({ id: 'seg-1', session: 'call-a' });

  assert.throws(
    () => ledger.append([{ text: 'alpha', segment: 'seg-1' }, { text: 'beta', segment: 'ghost' }]),
    /unknown segment/,
  );
  // The rejected append is atomic: nothing from it was recorded.
  assert.equal(ledger.tokenCount, 0);
  assert.deepEqual(counts(ledger), {});
});

test('duplicate segment ids and malformed privacy flags are rejected', () => {
  const ledger = new BigramLedger();
  ledger.defineSegment({ id: 'seg-1', session: 'call-a' });

  assert.throws(() => ledger.defineSegment({ id: 'seg-1', session: 'call-a' }), /duplicate segment id/);
  assert.throws(
    () => ledger.defineSegment({ id: 'seg-2', session: 'call-a', private: 'yes' }),
    /segment\.private must be a boolean/,
  );
  assert.throws(
    () => ledger.defineSegment({ id: 'seg-3', session: '' }),
    /segment\.session must be a non-empty string/,
  );
});
