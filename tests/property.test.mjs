import { test } from 'node:test';
import assert from 'node:assert/strict';

import { AdapterRegistry } from '../src/registry.mjs';
import { SlotExhaustedError, StaleChunkError } from '../src/errors.mjs';
import { weightsFor } from './support/fixtures.mjs';

const DIM = 3;

/** Small deterministic PRNG so failures are reproducible. */
function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

test('randomized interleaving keeps slot accounting and version purity', () => {
  for (let seed = 1; seed <= 60; seed++) {
    const rand = mulberry32(seed);
    const slots = 2 + Math.floor(rand() * 12);
    const registry = new AdapterRegistry({ slots });

    const sessions = []; // { session, weightsTag }
    const attempts = []; // { attempt, version, remaining, dead }
    const deadAttempts = [];
    let nextVersion = 1;
    let maxUsed = 0;
    let publishedCount = 0;

    const pick = (arr) => arr[Math.floor(rand() * arr.length)];

    for (let step = 0; step < 200; step++) {
      const roll = rand();

      // Slot conservation after every step.
      assert.equal(registry.pool.used + registry.pool.free, slots, `seed ${seed} step ${step}`);

      if (roll < 0.18 && attempts.length === 0) {
        const version = nextVersion++;
        const blockCount = 1 + Math.floor(rand() * 3);
        const blocks = Array.from({ length: blockCount }, (_, i) => i * 2 + version % 2);
        const slotCount = blockCount + Math.floor(rand() * 2);
        try {
          const attempt = registry.beginLoad({ version, blocks, slotCount });
          const remaining = new Set(blocks);
          attempts.push({ attempt, version, blocks, remaining, next: 0 });
        } catch (err) {
          assert.ok(err instanceof SlotExhaustedError, `seed ${seed}: ${err.message}`);
        }
      } else if (roll < 0.45 && attempts.length > 0) {
        const entry = attempts[0];
        // Deliver exactly one pending block.
        const pending = [...entry.remaining];
        const blockId = pending[Math.floor(rand() * pending.length)];
        entry.remaining.delete(blockId);
        const wasLast = entry.remaining.size === 0;
        const beforePublished = registry.publishedVersion;
        entry.attempt.deliver(blockId, weightsFor(entry.version, blockId, DIM));
        if (wasLast) {
          attempts.length = 0;
          publishedCount += 1;
          assert.equal(registry.publishedVersion, entry.version);
          assert.notEqual(registry.published, null);
        } else {
          assert.equal(registry.publishedVersion, beforePublished, 'partial load must not publish');
        }
      } else if (roll < 0.55 && attempts.length > 0) {
        const entry = attempts.shift();
        entry.attempt.abort('random abort');
        deadAttempts.push(entry);
        assert.equal(registry.activeAttempt, null);
      } else if (roll < 0.65 && deadAttempts.length > 0) {
        // Replay a late chunk from a dead attempt: must always be rejected
        // and must never publish or fill the current attempt.
        const entry = pick(deadAttempts);
        const blockId = entry.blocks[0];
        const before = registry.publishedVersion;
        assert.throws(
          () => registry.deliver(entry.attempt.id, blockId, weightsFor(999, blockId, DIM)),
          StaleChunkError,
          `seed ${seed} step ${step}`,
        );
        assert.equal(registry.publishedVersion, before);
      } else if (roll < 0.88) {
        if (registry.published !== null) {
          const session = registry.openSession();
          sessions.push({ session, version: session.version, generation: session.generation });
        }
      } else if (sessions.length > 0) {
        const i = Math.floor(rand() * sessions.length);
        const [entry] = sessions.splice(i, 1);
        entry.session.end();
      }

      maxUsed = Math.max(maxUsed, registry.pool.used);
      assert.ok(registry.pool.used <= slots, `seed ${seed} overcommitted`);

      // Every open session still reports the version it opened with and
      // scores only with that version's weights.
      for (const { session, version, generation } of sessions) {
        if (session.closed) continue;
        const result = session.score([1, 2, 3]);
        assert.equal(result.version, version);
        assert.equal(result.generation, generation);
        for (const block of result.blocks) {
          assert.equal(Math.trunc(block.weights[0] / 100), version, `seed ${seed}`);
        }
      }
    }

    // Tear everything down and confirm the pool fully recovers.
    for (const entry of attempts) entry.attempt.abort('teardown');
    for (const { session } of sessions) session.end();
    attempts.length = 0;
    const expectedUsed = registry.published === null ? 0 : registry.published.slotCount;
    assert.equal(registry.pool.used, expectedUsed, `seed ${seed} pool residue`);
    assert.equal(registry.retiredVersions.length, 0, `seed ${seed} retired residue`);
    assert.equal(registry.sessionCount, 0);
    assert.ok(publishedCount > 0, `seed ${seed} never published`);
    assert.ok(maxUsed <= slots);
  }
});
