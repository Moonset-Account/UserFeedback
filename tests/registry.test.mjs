import { test } from 'node:test';
import assert from 'node:assert/strict';

import { AdapterRegistry } from '../src/registry.mjs';
import {
  DuplicateChunkError,
  LoadInFlightError,
  SlotExhaustedError,
  StaleChunkError,
  UnknownBlockError,
} from '../src/errors.mjs';
import {
  deliverAll,
  expectedScore,
  loadAndPublish,
  manifestFor,
  weightsFor,
} from './support/fixtures.mjs';

const INPUT = [2, -1];
const DIM = 2;

function registryWithV1({ slots = 4, blocks = [0, 1], version = 1 } = {}) {
  const registry = new AdapterRegistry({ slots });
  loadAndPublish(registry, version, blocks, DIM);
  return registry;
}

// ---------------------------------------------------------------------------
// Normal path
// ---------------------------------------------------------------------------

test('a fully delivered load publishes atomically and serves sessions', () => {
  const registry = new AdapterRegistry({ slots: 2 });
  assert.equal(registry.publishedVersion, null);
  assert.equal(registry.pool.used, 0);

  const attempt = registry.beginLoad(manifestFor(1, [1, 0]));
  assert.equal(registry.publishedVersion, null, 'nothing is visible before the last chunk');
  assert.equal(registry.pool.used, 2, 'all slots are taken at admission');

  attempt.deliver(1, weightsFor(1, 1, DIM));
  assert.equal(registry.publishedVersion, null, 'partial loads are never published');
  attempt.deliver(0, weightsFor(1, 0, DIM));
  assert.equal(registry.publishedVersion, 1);
  assert.equal(attempt.status, 'published');
  assert.equal(registry.activeAttempt, null);

  const session = registry.openSession();
  const result = session.score(INPUT);
  assert.equal(result.version, 1);
  assert.equal(result.score, expectedScore(1, [0, 1], INPUT, DIM));
  // Published weights are frozen copies, not the delivered arrays.
  assert.notEqual(registry.published.block(0), weightsFor(1, 0, DIM));
  assert.deepEqual(registry.published.block(0), weightsFor(1, 0, DIM));
  assert.equal(Object.isFrozen(registry.published.block(0)), true);
});

test('delivered weights are copied; mutating the source array changes nothing', () => {
  const registry = new AdapterRegistry({ slots: 1 });
  const source = [5, 6];
  const attempt = registry.beginLoad(manifestFor(1, [0]));
  attempt.deliver(0, source);
  source[0] = 999;
  assert.deepEqual(registry.published.block(0), [5, 6]);
});

// ---------------------------------------------------------------------------
// The core requirement: sessions pin the version they opened with
// ---------------------------------------------------------------------------

test('a session keeps scoring with its opening version across a publish', () => {
  const registry = registryWithV1();
  const oldSession = registry.openSession();
  const before = oldSession.score(INPUT);

  assert.equal(before.version, 1);
  assert.equal(before.score, expectedScore(1, [0, 1], INPUT, DIM));

  // A new version publishes while the old session is mid-conversation.
  loadAndPublish(registry, 2, [0, 1], DIM);
  assert.equal(registry.publishedVersion, 2);

  const after = oldSession.score(INPUT);
  assert.equal(after.version, 1, 'old session must not observe the new version');
  assert.equal(after.score, before.score, 'same input, same pinned weights');
  assert.deepEqual(
    after.blocks.map((b) => b.weights),
    before.blocks.map((b) => b.weights),
  );
});

test('a session opened mid-load gets the pre-load published version', () => {
  const registry = registryWithV1();
  const attempt = registry.beginLoad(manifestFor(2, [0, 1]));
  attempt.deliver(0, weightsFor(2, 0, DIM));

  const duringLoad = registry.openSession();
  assert.equal(duringLoad.version, 1, 'loading must not disturb serving');

  attempt.deliver(1, weightsFor(2, 1, DIM));
  assert.equal(registry.publishedVersion, 2);

  const afterPublish = duringLoad.score(INPUT);
  assert.equal(afterPublish.version, 1);
  assert.equal(afterPublish.score, expectedScore(1, [0, 1], INPUT, DIM));

  const fresh = registry.openSession();
  const freshResult = fresh.score(INPUT);
  assert.equal(freshResult.version, 2);
  assert.equal(freshResult.score, expectedScore(2, [0, 1], INPUT, DIM));
});

test('interleaved sessions across two publishes never mix weight generations', () => {
  // Each generation pins 2 slots; a third load needs 2 more while v1 and v2
  // are both still pinned, so 8 slots are required.
  const registry = new AdapterRegistry({ slots: 8 });
  loadAndPublish(registry, 1, [0, 1], DIM);

  const a = registry.openSession(); // v1
  const aFirst = a.score(INPUT);

  loadAndPublish(registry, 2, [0, 1], DIM);
  const b = registry.openSession(); // opens between the two publishes
  const bFirst = b.score(INPUT);

  loadAndPublish(registry, 3, [0, 1], DIM);
  const aSecond = a.score([3, 4]);
  const bSecond = b.score([3, 4]);
  const c = registry.openSession(); // v3
  const cFirst = c.score(INPUT);

  assert.equal(aFirst.version, 1);
  assert.equal(aSecond.version, 1);
  assert.equal(bFirst.version, 2);
  assert.equal(bSecond.version, 2);
  assert.equal(cFirst.version, 3);

  assert.equal(aSecond.score, expectedScore(1, [0, 1], [3, 4], DIM));
  assert.equal(bSecond.score, expectedScore(2, [0, 1], [3, 4], DIM));
  assert.equal(cFirst.score, expectedScore(3, [0, 1], INPUT, DIM));

  // Every score reports a single version and weights drawn only from it.
  for (const result of [aFirst, aSecond, bFirst, bSecond, cFirst]) {
    const w0 = result.blocks[0].weights;
    for (const entry of result.blocks) {
      assert.equal(entry.weights[0], result.version * 100 + entry.blockId * 10);
      assert.equal(entry.weights.length, w0.length);
    }
  }

  // A mixed-generation score would land between the pure expectations.
  const mixed = (aSecond.score + cFirst.score) / 2;
  assert.notEqual(aSecond.score, mixed);
});

test('a retired version stays readable to pinned sessions after republish of same number', () => {
  const registry = new AdapterRegistry({ slots: 2 });
  loadAndPublish(registry, 1, [0], DIM);
  const pinned = registry.openSession();
  // Same numeric version, different weights: a distinct generation.
  const attempt = registry.beginLoad(manifestFor(1, [0]));
  attempt.deliver(0, [7, 7]);
  assert.equal(registry.publishedVersion, 1);
  assert.equal(registry.publishedGeneration, 1);

  assert.deepEqual(pinned.adapter.block(0), weightsFor(1, 0, DIM));
  assert.deepEqual(registry.published.block(0), [7, 7]);
  assert.equal(pinned.score(INPUT).generation, 0);
  assert.equal(registry.openSession().score(INPUT).generation, 1);
});

// ---------------------------------------------------------------------------
// Slot lifecycle
// ---------------------------------------------------------------------------

test('a load that cannot fit is refused before touching the published version', () => {
  const registry = registryWithV1({ slots: 4, blocks: [0, 1] }); // v1 holds 2
  const session = registry.openSession();
  assert.equal(registry.pool.free, 2);

  assert.throws(() => registry.beginLoad(manifestFor(2, [0, 1, 2])), SlotExhaustedError);
  assert.equal(registry.activeAttempt, null);
  assert.equal(registry.pool.free, 2);
  assert.equal(registry.publishedVersion, 1);
  assert.equal(session.score(INPUT).version, 1);
});

test('atomic admission: a load that cannot fit reserves nothing', () => {
  const registry = registryWithV1({ slots: 3, blocks: [0, 1] }); // 1 slot free
  assert.throws(() => registry.beginLoad(manifestFor(2, [0, 1])), SlotExhaustedError);
  assert.equal(registry.pool.used, 2, 'no partial reservation leaked');
});

test('a failed load releases exactly its own slots', () => {
  const registry = registryWithV1({ slots: 5, blocks: [0, 1] }); // v1 holds 2, 3 free
  const attempt = registry.beginLoad(manifestFor(2, [0, 1, 2]));
  attempt.deliver(0, weightsFor(2, 0, DIM));
  assert.equal(registry.pool.free, 0);

  attempt.abort('caller cancelled');
  assert.equal(registry.pool.free, 3, 'attempt slots returned');
  assert.equal(registry.publishedVersion, 1, 'published version untouched');
  assert.equal(attempt.status, 'aborted');
  assert.equal(registry.pool.used, 2, 'published version still holds its own slots');
});

test('a protocol violation aborts the load and frees its slots', () => {
  const registry = registryWithV1({ slots: 4, blocks: [0, 1] });
  const attempt = registry.beginLoad(manifestFor(2, [0, 1]));
  attempt.deliver(0, weightsFor(2, 0, DIM));
  assert.throws(() => attempt.deliver(0, weightsFor(2, 0, DIM)), DuplicateChunkError);
  assert.equal(attempt.status, 'aborted');
  assert.equal(registry.pool.free, 2);
});

test('unknown block and wrong dimension abort the load', () => {
  const registry = registryWithV1({ slots: 4, blocks: [0, 1] });

  const a = registry.beginLoad(manifestFor(2, [0, 1]));
  assert.throws(() => a.deliver(9, [1, 1]), UnknownBlockError);
  assert.equal(registry.pool.free, 2);

  const b = registry.beginLoad(manifestFor(2, [0, 1]));
  b.deliver(0, [1, 2]);
  assert.throws(() => b.deliver(1, [1, 2, 3]), RangeError);
  assert.equal(registry.pool.free, 2);
});

test('non-integer weights are refused and abort the load', () => {
  const registry = registryWithV1({ slots: 3, blocks: [0] });
  const attempt = registry.beginLoad(manifestFor(2, [0]));
  assert.throws(() => attempt.deliver(0, [1.5]), TypeError);
  assert.equal(attempt.status, 'aborted');
  assert.equal(registry.pool.free, 2);
});

test('only one load may be in flight', () => {
  const registry = registryWithV1({ slots: 4, blocks: [0, 1] });
  const first = registry.beginLoad(manifestFor(2, [0, 1]));
  assert.throws(() => registry.beginLoad(manifestFor(3, [0, 1])), LoadInFlightError);
  assert.equal(registry.pool.used, 4);
  first.abort();
  // After the abort a retry may start.
  const retry = registry.beginLoad(manifestFor(3, [0, 1]));
  assert.equal(retry.id, first.id + 1);
  retry.abort();
});

// ---------------------------------------------------------------------------
// Late chunks from dead attempts must never enter a retry
// ---------------------------------------------------------------------------

test('a late chunk from an aborted attempt is rejected', () => {
  const registry = registryWithV1({ slots: 4, blocks: [0, 1] });
  const dead = registry.beginLoad(manifestFor(2, [0, 1]));
  dead.deliver(0, weightsFor(2, 0, DIM));
  dead.abort('timed out');

  assert.throws(() => dead.deliver(1, weightsFor(2, 1, DIM)), StaleChunkError);
  assert.throws(() => registry.deliver(dead.id, 1, weightsFor(2, 1, DIM)), StaleChunkError);
  assert.equal(registry.publishedVersion, 1);
});

test('a late chunk cannot complete a retry that reused nothing from the dead attempt', () => {
  const registry = registryWithV1({ slots: 4, blocks: [0, 1] });

  const dead = registry.beginLoad(manifestFor(2, [0, 1]));
  dead.deliver(0, [111, 111]);
  dead.abort('connection lost');

  const retry = registry.beginLoad(manifestFor(2, [0, 1]));
  // A chunk stamped with the dead attempt id arrives while the retry runs.
  assert.throws(() => dead.deliver(1, [222, 222]), StaleChunkError);
  assert.throws(() => registry.deliver(dead.id, 1, [222, 222]), StaleChunkError);
  assert.deepEqual(retry.receivedBlockIds, [], 'retry received nothing from the dead attempt');
  assert.equal(registry.publishedVersion, 1, 'a stale chunk must not publish anything');

  // Redelivering block 0 for the retry is a fresh chunk, not a duplicate.
  retry.deliver(0, weightsFor(2, 0, DIM));
  retry.deliver(1, weightsFor(2, 1, DIM));
  assert.equal(registry.publishedVersion, 2);
  assert.deepEqual(registry.published.block(0), weightsFor(2, 0, DIM));
  assert.deepEqual(registry.published.block(1), weightsFor(2, 1, DIM));
});

test('a chunk from a completed attempt is stale once a new attempt starts', () => {
  const registry = new AdapterRegistry({ slots: 2 });
  const published = registry.beginLoad(manifestFor(1, [0]));
  published.deliver(0, weightsFor(1, 0, DIM));

  const next = registry.beginLoad(manifestFor(2, [0]));
  assert.throws(
    () => registry.deliver(published.id, 0, weightsFor(1, 0, DIM)),
    StaleChunkError,
  );
  assert.deepEqual(next.receivedBlockIds, []);
  assert.equal(registry.publishedVersion, 1);
});

// ---------------------------------------------------------------------------
// Reclaiming retired versions
// ---------------------------------------------------------------------------

test('a retired version is reclaimed only when its last pinned session ends', () => {
  const registry = registryWithV1({ slots: 4, blocks: [0, 1] }); // v1 holds 2
  const s1 = registry.openSession();
  const s2 = registry.openSession();
  assert.equal(registry.pool.used, 2);

  loadAndPublish(registry, 2, [0, 1], DIM); // needs 2 free slots: fits exactly
  assert.equal(registry.pool.used, 4, 'v2 holds its slots, v1 still pinned');
  assert.deepEqual(
    registry.retiredVersions.map((r) => [r.version, r.pins]),
    [[1, 2]],
  );

  s1.end();
  assert.equal(registry.pool.used, 4, 'v1 still pinned by s2');
  assert.deepEqual(
    registry.retiredVersions.map((r) => [r.version, r.pins]),
    [[1, 1]],
  );

  s2.end();
  assert.equal(registry.pool.used, 2, 'v1 slots released with its last pin');
  assert.deepEqual(registry.retiredVersions, []);
  assert.equal(registry.pool.free, 2);
});

test('a published version with no sessions is reclaimed immediately on replacement', () => {
  const registry = new AdapterRegistry({ slots: 2 });
  loadAndPublish(registry, 1, [0], DIM);
  assert.equal(registry.pool.used, 1);
  loadAndPublish(registry, 2, [0], DIM);
  assert.equal(registry.pool.used, 1, 'v1 had no pins, so it let go at once');
  assert.deepEqual(registry.retiredVersions, []);
});

test('ending a session twice is a no-op and cannot free slots early', () => {
  const registry = new AdapterRegistry({ slots: 4 });
  loadAndPublish(registry, 1, [0, 1], DIM);
  const s1 = registry.openSession();
  const s2 = registry.openSession();
  loadAndPublish(registry, 2, [0, 1], DIM);

  assert.equal(s1.end(), true);
  assert.equal(s1.end(), false);
  assert.equal(registry.pool.used, 4, 'v1 is still pinned by s2');
  assert.deepEqual(
    registry.retiredVersions.map((r) => r.pins),
    [1],
  );
});

test('a closed session refuses to score and cannot resurrect a reclaimed version', () => {
  const registry = registryWithV1({ slots: 2, blocks: [0] });
  const session = registry.openSession();
  loadAndPublish(registry, 2, [0], DIM);
  session.end();
  assert.throws(() => session.score(INPUT), /closed/);
  assert.equal(registry.pool.used, 1);
});

// ---------------------------------------------------------------------------
// Boundary shapes
// ---------------------------------------------------------------------------

test('a version may declare more slots than it has blocks', () => {
  const registry = new AdapterRegistry({ slots: 5 });
  const attempt = registry.beginLoad(manifestFor(1, [0, 1], 4));
  assert.equal(registry.pool.used, 4, 'declared slot count is what gets reserved');
  deliverAll(attempt, 1, DIM);
  assert.equal(registry.published.slotCount, 4);
  assert.equal(registry.pool.used, 4);
});

test('a single-block, single-dimension version works end to end', () => {
  const registry = new AdapterRegistry({ slots: 1 });
  loadAndPublish(registry, 1, [0], 1);
  const session = registry.openSession();
  assert.equal(session.score([-3]).score, expectedScore(1, [0], [-3], 1));
});

test('blocks publish in ascending order regardless of delivery order', () => {
  const registry = new AdapterRegistry({ slots: 3 });
  const attempt = registry.beginLoad(manifestFor(1, [7, 2, 5]));
  attempt.deliver(7, weightsFor(1, 7, DIM));
  attempt.deliver(2, weightsFor(1, 2, DIM));
  attempt.deliver(5, weightsFor(1, 5, DIM));
  assert.deepEqual(registry.published.blockIds, [2, 5, 7]);
  const session = registry.openSession();
  assert.deepEqual(
    session.score(INPUT).blocks.map((b) => b.blockId),
    [2, 5, 7],
  );
});

test('scoring before any publish is refused', () => {
  const registry = new AdapterRegistry({ slots: 1 });
  assert.throws(() => registry.openSession(), /no published adapter version/);
});

test('invalid registries and malformed input are rejected', () => {
  assert.throws(() => new AdapterRegistry({ slots: 0 }), RangeError);
  assert.throws(() => new AdapterRegistry({}), RangeError);
  const registry = registryWithV1({ slots: 2, blocks: [0] });
  const session = registry.openSession();
  assert.throws(() => session.score('nope'), TypeError);
  assert.throws(() => session.score([1, 2, 3]), RangeError, 'dimension mismatch vs frozen weights');
});

// ---------------------------------------------------------------------------
// Observability
// ---------------------------------------------------------------------------

test('events record the full publish lifecycle', () => {
  const registry = new AdapterRegistry({ slots: 4 });
  loadAndPublish(registry, 1, [0, 1], DIM);
  const session = registry.openSession();
  loadAndPublish(registry, 2, [0, 1], DIM);
  session.end();

  assert.deepEqual(
    registry.events.map((e) => e.type),
    [
      'load-started',
      'version-published',
      'load-started',
      'version-retired',
      'version-published',
      'session-ended',
      'version-reclaimed',
    ],
  );
  assert.deepEqual(
    registry.publishLog().map((p) => [p.version, p.generation]),
    [
      [1, 0],
      [2, 1],
    ],
  );
});
