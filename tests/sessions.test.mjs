import { test } from 'node:test';
import assert from 'node:assert/strict';

import { AdapterRegistry } from '../src/registry.mjs';
import { weightsFor, expectedScore } from './support/fixtures.mjs';

const DIM = 2;
const INPUT = [3, 5];

function publish(registry, version, blocks) {
  const attempt = registry.beginLoad({ version, blocks, slotCount: blocks.length });
  // Deliver out of order on purpose; publication waits for the last chunk.
  for (const blockId of [...blocks].reverse()) {
    attempt.deliver(blockId, weightsFor(version, blockId, DIM));
  }
  return attempt;
}

test('the same session scoring twice across a publish is byte-identical', () => {
  const registry = new AdapterRegistry({ slots: 6 });
  publish(registry, 1, [0, 1]);

  const pinned = registry.openSession();
  const first = pinned.score(INPUT);

  publish(registry, 2, [0, 1]); // new session opens only after this returns

  const second = pinned.score(INPUT);

  assert.equal(first.version, 1);
  assert.equal(second.version, 1);
  assert.equal(first.score, second.score);
  assert.deepEqual(
    first.blocks.map((b) => b.weights),
    second.blocks.map((b) => b.weights),
  );
  // And the neutral fact: both equal the pure v1 expectation.
  assert.equal(second.score, expectedScore(1, [0, 1], INPUT, DIM));
});

test('a session started between the two scores gets the new version only', () => {
  const registry = new AdapterRegistry({ slots: 6 });
  publish(registry, 1, [0, 1]);
  const oldSession = registry.openSession();
  const oldScore = oldSession.score(INPUT);

  publish(registry, 2, [0, 1]);
  const newSession = registry.openSession(); // opens in the middle
  const newScore = newSession.score(INPUT);

  assert.equal(oldScore.version, 1);
  assert.equal(newScore.version, 2);
  assert.equal(newScore.score, expectedScore(2, [0, 1], INPUT, DIM));

  // Re-score the old session once more after the new one has run.
  const oldAgain = oldSession.score(INPUT);
  assert.equal(oldAgain.version, 1);
  assert.equal(oldAgain.score, oldScore.score);

  // A mixed-generation result would have contributions from both versions.
  const leadingDigits = (result) => result.blocks.map((b) => Math.trunc(b.weights[0] / 100));
  assert.deepEqual(leadingDigits(oldAgain), [1, 1]);
  assert.deepEqual(leadingDigits(newScore), [2, 2]);
});

test('three generations coexist: v1 pinned, v2 pinned, v3 current', () => {
  const registry = new AdapterRegistry({ slots: 9 });
  publish(registry, 1, [0]);
  const s1 = registry.openSession();
  const r1 = s1.score(INPUT);

  publish(registry, 2, [0]);
  const s2 = registry.openSession();
  const r2 = s2.score(INPUT);

  publish(registry, 3, [0]);
  const s3 = registry.openSession();
  const r3 = s3.score(INPUT);

  assert.deepEqual([r1.version, r2.version, r3.version], [1, 2, 3]);
  assert.deepEqual(
    [r1.score, r2.score, r3.score],
    [expectedScore(1, [0], INPUT, DIM), expectedScore(2, [0], INPUT, DIM), expectedScore(3, [0], INPUT, DIM)],
  );
  assert.equal(registry.pool.used, 3 * 1 + 0);
  assert.deepEqual(
    registry.retiredVersions.map((r) => [r.version, r.pins]),
    [
      [1, 1],
      [2, 1],
    ],
  );
});

test('a session sees a stable snapshot even if weights arrive out of order', () => {
  // v1 pins 3 slots and v2's load needs 3 while v1 is still pinned.
  const registry = new AdapterRegistry({ slots: 8 });
  publish(registry, 1, [0, 1, 2]);
  const session = registry.openSession();
  const before = session.score(INPUT);

  // Publish a new version whose blocks arrive in scrambled order.
  const attempt = registry.beginLoad({ version: 2, blocks: [3, 1, 2], slotCount: 3 });
  attempt.deliver(3, weightsFor(2, 3, DIM));
  attempt.deliver(2, weightsFor(2, 2, DIM));
  assert.equal(registry.publishedVersion, 1, 'still serving v1 until the last chunk');
  const midScore = session.score(INPUT);
  assert.equal(midScore.score, before.score);
  attempt.deliver(1, weightsFor(2, 1, DIM));

  assert.equal(registry.publishedVersion, 2);
  assert.deepEqual(registry.published.blockIds, [1, 2, 3]);
  assert.equal(session.score(INPUT).score, before.score);
});

test('async chunk arrival keeps the same invariant', async () => {
  const registry = new AdapterRegistry({ slots: 4 });
  publish(registry, 1, [0, 1]);
  const pinned = registry.openSession();
  const baseline = pinned.score(INPUT);

  const attempt = registry.beginLoad({ version: 2, blocks: [0, 1], slotCount: 2 });
  const order = [];
  const deliver = (blockId) =>
    new Promise((resolve) => {
      setImmediate(() => {
        // Score between chunk arrivals, exactly like a live request.
        const interimVersion = pinned.score(INPUT).version;
        assert.equal(interimVersion, 1);
        attempt.deliver(blockId, weightsFor(2, blockId, DIM));
        order.push(blockId);
        resolve();
      });
    });

  await deliver(0);
  assert.equal(registry.publishedVersion, 1);
  await deliver(1);

  assert.deepEqual(order, [0, 1]);
  assert.equal(registry.publishedVersion, 2);
  const after = pinned.score(INPUT);
  assert.equal(after.version, 1);
  assert.equal(after.score, baseline.score);
  assert.equal(registry.openSession().score(INPUT).version, 2);
});
