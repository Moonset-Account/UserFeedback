import test from 'node:test';
import assert from 'node:assert/strict';

import { BigramLedger, buildFromScratch, replay } from '../src/bigram-ledger.js';

/**
 * Small deterministic PRNG so the property test is reproducible from a seed
 * without any external dependency.
 */
function mulberry32(seed) {
  let state = seed >>> 0;
  return function next() {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function pick(random, items) {
  return items[Math.floor(random() * items.length)];
}

/** Generate a random transcript plus a random retraction subset. */
function generateScenario(seed) {
  const random = mulberry32(seed);
  const segmentCount = 2 + Math.floor(random() * 6);
  const sessions = ['call-a', 'call-b', 'call-c'];
  const vocabulary = ['alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta', 'eta'];

  const segments = [];
  const tokens = [];
  for (let index = 0; index < segmentCount; index += 1) {
    const id = `seg-${index}`;
    const session = pick(random, sessions);
    const initiallyPrivate = random() < 0.15;
    const size = 1 + Math.floor(random() * 4);
    segments.push({
      id,
      session,
      ...(initiallyPrivate ? { private: true } : {}),
    });
    for (let tokenIndex = 0; tokenIndex < size; tokenIndex += 1) {
      tokens.push({ text: pick(random, vocabulary), segment: id });
    }
  }

  // Retract a random subset of the segments that were not private from birth.
  const retractable = segments.filter((segment) => segment.private !== true);
  const retracted = retractable.filter(() => random() < 0.4).map((segment) => segment.id);

  // Interleave the appends to exercise the barrier reading at different times.
  const split = Math.floor(tokens.length / 2);
  const order = random() < 0.5
    ? [{ kind: 'tokens', value: tokens }]
    : [
        { kind: 'tokens', value: tokens.slice(0, split) },
        { kind: 'retract' },
        { kind: 'tokens', value: tokens.slice(split) },
      ];

  return { segments, tokens, retracted, order };
}

function runLive(scenario) {
  const ledger = new BigramLedger();
  for (const segment of scenario.segments) {
    ledger.defineSegment({
      id: segment.id,
      session: segment.session,
      private: segment.private,
    });
  }
  const pending = scenario.order;
  const retractAll = () => {
    for (const id of scenario.retracted) {
      ledger.markPrivate(id);
    }
  };
  let didRetract = false;
  for (const step of pending) {
    if (step.kind === 'tokens') {
      ledger.append(step.value);
    } else {
      retractAll();
      didRetract = true;
    }
  }
  if (!didRetract) {
    retractAll();
  }
  return ledger;
}

test('randomised replay equals a from-scratch build over many seeds', () => {
  for (let seed = 1; seed <= 250; seed += 1) {
    const scenario = generateScenario(seed);
    const live = runLive(scenario);

    const scratch = buildFromScratch(
      { segments: scenario.segments, tokens: scenario.tokens },
      { privateSegmentIds: scenario.retracted },
    );
    const replayed = replay(live.events());

    assert.deepEqual(
      replayed.query(),
      scratch.query(),
      `replay mismatch at seed ${seed}`,
    );
    assert.deepEqual(
      live.query(),
      scratch.query(),
      `live mismatch at seed ${seed}`,
    );

    // Every candidate must have a strictly positive count.
    for (const candidate of live.query()) {
      assert.ok(candidate.count > 0, `non-positive count at seed ${seed}`);
    }
  }
});

/**
 * Independent, deliberately naive oracle: walk the raw token stream once and
 * count every adjacency whose two endpoints are public under the final flags
 * and share a session. It shares no code with the ledger, so agreement is
 * meaningful.
 */
function oracleCounts(segments, tokens, privateSegmentIds) {
  const isPrivate = new Map();
  for (const segment of segments) {
    isPrivate.set(segment.id, segment.private === true);
  }
  for (const id of privateSegmentIds) {
    isPrivate.set(id, true);
  }
  const session = new Map(segments.map((segment) => [segment.id, segment.session]));

  const tally = new Map();
  for (let index = 1; index < tokens.length; index += 1) {
    const previous = tokens[index - 1];
    const current = tokens[index];
    if (isPrivate.get(previous.segment) || isPrivate.get(current.segment)) continue;
    if (session.get(previous.segment) !== session.get(current.segment)) continue;
    const key = `${previous.text} ${current.text}`;
    tally.set(key, (tally.get(key) ?? 0) + 1);
  }
  return Object.fromEntries(tally);
}

test('randomised counts match an independent one-pass oracle', () => {
  for (let seed = 1; seed <= 250; seed += 1) {
    const scenario = generateScenario(seed);
    const live = runLive(scenario);

    const expected = oracleCounts(
      scenario.segments,
      scenario.tokens,
      scenario.retracted,
    );
    const actual = Object.fromEntries(
      live.query().map((candidate) => [candidate.bigram.join(' '), candidate.count]),
    );

    assert.deepEqual(actual, expected, `oracle mismatch at seed ${seed}`);
  }
});
