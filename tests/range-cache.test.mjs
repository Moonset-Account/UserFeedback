import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RangeCache, parseETag } from '../src/range-cache.js';

const PATH = '/videos/episode-01.m4s';
const ETAG = '"v1-abc"';
const ENC_GZIP = 'gzip';
const TOTAL = 100;

// Build a deterministic 100-byte representation where byte i === i.
function fullBody(size = TOTAL) {
  return Buffer.from(Array.from({ length: size }, (_, i) => i % 256));
}

function slice(start, end, body = fullBody()) {
  return body.subarray(start, end + 1);
}

function expectRuns(actual, expected) {
  assert.deepEqual(
    actual.map((r) => [r.start, r.end]),
    expected,
  );
}

test('parseETag distinguishes strong and weak validators', () => {
  assert.deepEqual(parseETag('"abc"'), { weak: false, opaque: 'abc' });
  assert.deepEqual(parseETag('W/"abc"'), { weak: true, opaque: 'abc' });
  assert.throws(() => parseETag('no-quotes'), RangeError);
  assert.throws(() => parseETag('"bad"quote"'), RangeError);
});

test('acceptable: strong fragments stitch across adjacent and overlapping intervals', () => {
  const cache = new RangeCache();
  const body = fullBody();

  let r = cache.put(PATH, slice(0, 29), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 0,
    end: 29,
  });
  assert.equal(r.status, 'stored');

  r = cache.put(PATH, slice(30, 69), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 30,
    end: 69,
  });
  assert.equal(r.status, 'stored');

  // Overlapping write carrying identical bytes plus fresh tail bytes: the
  // overlap matches and the new bytes extend coverage (no invalidation).
  r = cache.put(PATH, slice(60, 79), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 60,
    end: 79,
  });
  assert.equal(r.status, 'stored');

  // A purely redundant write of already-cached identical bytes merges.
  r = cache.put(PATH, slice(60, 69), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 60,
    end: 69,
  });
  assert.equal(r.status, 'merged');

  // Partial query: cached prefix + one continuous back-source gap.
  const partial = cache.query(PATH, {
    etag: ETAG,
    encoding: ENC_GZIP,
    start: 20,
    end: 89,
  });
  assert.equal(partial.hit, false);
  expectRuns(partial.hits, [[20, 79]]);
  expectRuns(partial.gaps, [[80, 89]]);
  assert.deepEqual(partial.data, slice(20, 79));

  // Fill the tail; the full request range must now reassemble exact bytes.
  cache.put(PATH, slice(80, 99), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 80,
    end: 99,
  });
  const full = cache.query(PATH, {
    etag: ETAG,
    encoding: ENC_GZIP,
    start: 0,
    end: 99,
  });
  assert.equal(full.hit, true);
  assert.deepEqual(full.hits, [{ start: 0, end: 99 }]);
  assert.deepEqual(full.gaps, []);
  assert.deepEqual(full.data, body);
});

test('blocked: overlapping byte conflict invalidates the strong representation', () => {
  const cache = new RangeCache();
  cache.put(PATH, slice(0, 49), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 0,
    end: 49,
  });

  const corrupt = slice(40, 69);
  corrupt[0] = 250; // offset 40 now disagrees with the cached byte.
  const r = cache.put(PATH, corrupt, {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 40,
    end: 69,
  });
  assert.equal(r.status, 'invalidated');
  assert.match(r.reason, /byte conflict/);

  // Queries must not serve any bytes from the invalidated representation.
  const q = cache.query(PATH, {
    etag: ETAG,
    encoding: ENC_GZIP,
    start: 0,
    end: 10,
  });
  assert.equal(q.hit, false);
  assert.equal(q.data, null);
  expectRuns(q.gaps, [[0, 10]]);

  // Further fragments are rejected until a trusted complete response arrives.
  const rejected = cache.put(PATH, slice(50, 60), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 50,
    end: 60,
  });
  assert.equal(rejected.status, 'rejected-invalid');

  const state = cache.getState(PATH, { etag: ETAG, encoding: ENC_GZIP });
  assert.equal(state.invalid, true);
  assert.equal(state.coveredCount, 0);
});

test('blocked: totalLength conflict under the same strong ETag invalidates', () => {
  const cache = new RangeCache();
  cache.put(PATH, slice(0, 29), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 0,
    end: 29,
  });

  const r = cache.put(PATH, Buffer.alloc(20), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: 120,
    start: 0,
    end: 19,
  });
  assert.equal(r.status, 'invalidated');
  assert.match(r.reason, /totalLength conflict/);

  const q = cache.query(PATH, {
    etag: ETAG,
    encoding: ENC_GZIP,
    start: 0,
    end: 9,
  });
  assert.equal(q.hit, false);
  assert.equal(q.data, null);
});

test('recovery: trusted complete response rebuilds an invalidated representation', () => {
  const cache = new RangeCache();
  const body = fullBody();

  const corrupt = slice(0, 9);
  corrupt[0] = 250;
  cache.put(PATH, corrupt, {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 0,
    end: 9,
  });
  cache.put(PATH, slice(0, 9), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 0,
    end: 9,
  });
  assert.equal(
    cache.getState(PATH, { etag: ETAG, encoding: ENC_GZIP }).invalid,
    true,
  );

  // An untrusted full payload is treated as an ordinary fragment and refused.
  const untrusted = cache.put(PATH, body, {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
  });
  assert.equal(untrusted.status, 'rejected-invalid');

  // A trusted full response rebuilds and whole-range queries hit immediately.
  const rebuilt = cache.put(PATH, body, {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    trusted: true,
  });
  assert.equal(rebuilt.status, 'rebuilt');

  const q = cache.query(PATH, {
    etag: ETAG,
    encoding: ENC_GZIP,
    start: 10,
    end: 40,
  });
  assert.equal(q.hit, true);
  assert.deepEqual(q.data, slice(10, 40));
});

test('weak fragments never stitch; only a single fully covering fragment hits', () => {
  const cache = new RangeCache();
  const weak = 'W/"temp-1"';

  cache.put(PATH, slice(0, 39), {
    etag: weak,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 0,
    end: 39,
  });
  cache.put(PATH, slice(40, 79), {
    etag: weak,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 40,
    end: 79,
  });

  // Two adjacent weak fragments do NOT stitch into a 0..79 hit.
  const stitched = cache.query(PATH, {
    etag: weak,
    encoding: ENC_GZIP,
    start: 0,
    end: 79,
  });
  assert.equal(stitched.hit, false);
  assert.equal(stitched.data, null);
  expectRuns(stitched.gaps, [[0, 79]]);

  // A single fragment fully covering the request range hits.
  const inside = cache.query(PATH, {
    etag: weak,
    encoding: ENC_GZIP,
    start: 10,
    end: 30,
  });
  assert.equal(inside.hit, true);
  assert.deepEqual(inside.data, slice(10, 30));

  // Boundary: request ending exactly at a fragment boundary still hits.
  const edge = cache.query(PATH, {
    etag: weak,
    encoding: ENC_GZIP,
    start: 40,
    end: 79,
  });
  assert.equal(edge.hit, true);
  assert.deepEqual(edge.data, slice(40, 79));
});

test('weak fragments: newest fully covering fragment wins', () => {
  const cache = new RangeCache();
  const weak = 'W/"temp-2"';
  const bodyA = Buffer.alloc(TOTAL, 1);
  const bodyB = Buffer.alloc(TOTAL, 2);

  cache.put(PATH, bodyA, {
    etag: weak,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
  });
  cache.put(PATH, bodyB, {
    etag: weak,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
  });

  const q = cache.query(PATH, {
    etag: weak,
    encoding: ENC_GZIP,
    start: 0,
    end: 99,
  });
  assert.equal(q.hit, true);
  assert.deepEqual(q.data, bodyB);
});

test('interleaved compressed and identity representations stay independent', () => {
  const cache = new RangeCache();
  const identity = Buffer.from(
    Array.from({ length: TOTAL }, (_, i) => 100 + (i % 100)),
  );
  const gzipped = fullBody();

  // Interleave writes to the same path under different content encodings.
  cache.put(PATH, gzipped.subarray(0, 30), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 0,
    end: 29,
  });
  cache.put(PATH, identity.subarray(0, 40), {
    etag: ETAG,
    totalLength: TOTAL,
    start: 0,
    end: 39,
  });
  cache.put(PATH, gzipped.subarray(30, 60), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 30,
    end: 59,
  });
  cache.put(PATH, identity.subarray(40, 70), {
    etag: ETAG,
    totalLength: TOTAL,
    start: 40,
    end: 69,
  });

  const gz = cache.query(PATH, {
    etag: ETAG,
    encoding: ENC_GZIP,
    start: 15,
    end: 55,
  });
  assert.equal(gz.hit, true);
  assert.deepEqual(gz.data, gzipped.subarray(15, 56));

  const id = cache.query(PATH, {
    etag: ETAG,
    start: 25,
    end: 65,
  });
  assert.equal(id.hit, true);
  assert.deepEqual(id.data, identity.subarray(25, 66));

  // Partial overlap produces runs whose concatenation rebuilds original bytes
  // in request-range order, with one gap describing the back-source work.
  cache.put(PATH, identity.subarray(80, 100), {
    etag: ETAG,
    totalLength: TOTAL,
    start: 80,
    end: 99,
  });
  const partial = cache.query(PATH, { etag: ETAG, start: 60, end: 89 });
  assert.equal(partial.hit, false);
  expectRuns(partial.hits, [[60, 69], [80, 89]]);
  expectRuns(partial.gaps, [[70, 79]]);
  const rebuilt = Buffer.concat([
    identity.subarray(60, 70),
    identity.subarray(80, 90),
  ]);
  assert.deepEqual(partial.data, rebuilt);

  // Invalidating one encoding must not poison the other.
  const corrupt = gzipped.subarray(50, 60);
  corrupt[0] = 255;
  cache.put(PATH, corrupt, {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 50,
    end: 59,
  });
  const idStillHit = cache.query(PATH, { etag: ETAG, start: 0, end: 39 });
  assert.equal(idStillHit.hit, true);
  const gzBlocked = cache.query(PATH, {
    etag: ETAG,
    encoding: ENC_GZIP,
    start: 0,
    end: 9,
  });
  assert.equal(gzBlocked.hit, false);
});

test('different strong ETags never stitch and remain isolated', () => {
  const cache = new RangeCache();
  const etag2 = '"v2-xyz"';

  cache.put(PATH, slice(0, 29), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 0,
    end: 29,
  });
  cache.put(PATH, Buffer.alloc(30, 7), {
    etag: etag2,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 0,
    end: 29,
  });

  const q1 = cache.query(PATH, {
    etag: ETAG,
    encoding: ENC_GZIP,
    start: 0,
    end: 39,
  });
  expectRuns(q1.hits, [[0, 29]]);
  expectRuns(q1.gaps, [[30, 39]]);
  assert.deepEqual(q1.data, slice(0, 29));

  const q2 = cache.query(PATH, {
    etag: etag2,
    encoding: ENC_GZIP,
    start: 0,
    end: 9,
  });
  assert.deepEqual(q2.data, Buffer.alloc(10, 7));
});

test('boundary: single-byte ranges, exact edges and out-of-length requests', () => {
  const cache = new RangeCache();
  cache.put(PATH, slice(0, 0), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 0,
    end: 0,
  });
  cache.put(PATH, slice(99, 99), {
    etag: ETAG,
    encoding: ENC_GZIP,
    totalLength: TOTAL,
    start: 99,
    end: 99,
  });

  const first = cache.query(PATH, {
    etag: ETAG,
    encoding: ENC_GZIP,
    start: 0,
    end: 0,
  });
  assert.equal(first.hit, true);
  assert.deepEqual(first.data, Buffer.from([0]));

  const last = cache.query(PATH, {
    etag: ETAG,
    encoding: ENC_GZIP,
    start: 99,
    end: 99,
  });
  assert.equal(last.hit, true);
  assert.deepEqual(last.data, Buffer.from([99]));

  // Request touching an uncached middle splits into alternating runs.
  const middle = cache.query(PATH, {
    etag: ETAG,
    encoding: ENC_GZIP,
    start: 0,
    end: 99,
  });
  assert.equal(middle.hit, false);
  expectRuns(middle.hits, [[0, 0], [99, 99]]);
  expectRuns(middle.gaps, [[1, 98]]);

  // Ranges beyond totalLength are never served.
  const beyond = cache.query(PATH, {
    etag: ETAG,
    encoding: ENC_GZIP,
    start: 99,
    end: 100,
  });
  assert.equal(beyond.hit, false);
  assert.equal(beyond.data, null);
  assert.match(beyond.reason, /exceeds/);
});

test('validation: malformed writes are rejected before mutating state', () => {
  const cache = new RangeCache();
  const base = { etag: ETAG, encoding: ENC_GZIP, totalLength: TOTAL, start: 0, end: 9 };

  assert.throws(
    () => cache.put(PATH, Buffer.alloc(5), base),
    /data length does not match/,
  );
  assert.throws(
    () => cache.put(PATH, slice(0, 9), { ...base, end: TOTAL }),
    /within totalLength/,
  );
  assert.throws(
    () => cache.put(PATH, slice(0, 9), { ...base, start: 5 }),
    /0 <= start <= end|data length/,
  );

  // Nothing was written: state remains absent.
  assert.equal(cache.getState(PATH, base), null);
});
