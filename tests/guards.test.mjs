import { test } from "node:test";
import assert from "node:assert/strict";

import { guardMatches, normalizeGuard, normalizeShape, requestKey } from "../src/guards.mjs";

const shape = (dims) => normalizeShape(dims);
const req = (dims, layout = "row-major", dtype = "float32") => ({
  modelVersion: "v1",
  shape: shape(dims),
  layout,
  dtype,
});

test("per-dimension ranges are inclusive on both boundaries", () => {
  const guard = normalizeGuard(
    {
      dims: [
        { min: 2, max: 8 },
        { min: 4, max: 16 },
      ],
    },
    2,
  );
  assert.equal(guardMatches(guard, shape([2, 4]), req([2, 4])), true);
  assert.equal(guardMatches(guard, shape([8, 16]), req([8, 16])), true);
  assert.equal(guardMatches(guard, shape([1, 4]), req([1, 4])), false);
  assert.equal(guardMatches(guard, shape([2, 17]), req([2, 17])), false);
});

test("equality clause fails even when each dimension is inside its own range", () => {
  // Both dims are independently legal; only the inter-dimension clause can reject [4, 8].
  const guard = normalizeGuard(
    {
      dims: [
        { min: 1, max: 16 },
        { min: 1, max: 16 },
      ],
      equal: [[0, 1]],
    },
    2,
  );
  assert.equal(guardMatches(guard, shape([4, 4]), req([4, 4])), true);
  assert.equal(guardMatches(guard, shape([4, 8]), req([4, 8])), false);
  assert.equal(guardMatches(guard, shape([8, 4]), req([8, 4])), false);
});

test("divisibility clause requires numerator to be a multiple of denominator", () => {
  const guard = normalizeGuard(
    {
      dims: [
        { min: 1, max: 32 },
        { min: 1, max: 32 },
      ],
      divisible: [{ numerator: 0, denominator: 1 }],
    },
    2,
  );
  assert.equal(guardMatches(guard, shape([8, 4]), req([8, 4])), true);
  assert.equal(guardMatches(guard, shape([6, 3]), req([6, 3])), true);
  assert.equal(guardMatches(guard, shape([7, 4]), req([7, 4])), false);
  // Again: both values sit in [1, 32], only the divisibility relationship fails.
});

test("equality and divisibility can be combined", () => {
  const guard = normalizeGuard(
    {
      equal: [[0, 1]],
      divisible: [{ numerator: 2, denominator: 0 }],
    },
    3,
  );
  assert.equal(guardMatches(guard, shape([4, 4, 8]), req([4, 4, 8])), true);
  assert.equal(guardMatches(guard, shape([4, 4, 6]), req([4, 4, 6])), false);
  assert.equal(guardMatches(guard, shape([4, 5, 8]), req([4, 5, 8])), false);
});

test("layout and dtype must match exactly when constrained", () => {
  const guard = normalizeGuard(
    {
      layout: "tiled",
      dtype: "float16",
    },
    1,
  );
  assert.equal(guardMatches(guard, shape([4]), req([4], "tiled", "float16")), true);
  assert.equal(guardMatches(guard, shape([4]), req([4], "row-major", "float16")), false);
  assert.equal(guardMatches(guard, shape([4]), req([4], "tiled", "float32")), false);
});

test("unconstrained layout/dtype and missing dims default to permissive", () => {
  const guard = normalizeGuard({}, 2);
  assert.equal(guard.layout, null);
  assert.equal(guard.dtype, null);
  assert.equal(guardMatches(guard, shape([1, 9007199254740991]), req([1, 9007199254740991])), true);
});

test("rank mismatch never matches", () => {
  const guard = normalizeGuard({ dims: [{ min: 1, max: 4 }] }, 1);
  assert.equal(guardMatches(guard, shape([1, 2]), req([1, 2])), false);
});

test("normalized guards are frozen and malformed guards are rejected", () => {
  const guard = normalizeGuard({ equal: [[0, 1]], dims: [{ min: 1, max: 2 }, { min: 1, max: 2 }] }, 2);
  assert.ok(Object.isFrozen(guard));
  assert.ok(Object.isFrozen(guard.equal[0]));

  assert.throws(() => normalizeGuard({ dims: [{ min: 0, max: 2 }] }, 1), RangeError);
  assert.throws(() => normalizeGuard({ dims: [{ min: 3, max: 2 }] }, 1), RangeError);
  assert.throws(() => normalizeGuard({ equal: [[0, 5]] }, 2), RangeError);
  assert.throws(() => normalizeGuard({ divisible: [{ numerator: 0, denominator: -1 }] }, 2), RangeError);
  assert.throws(() => normalizeGuard({ dims: [1, 2] }, 2), TypeError);
});

test("shape normalization rejects non-positive or non-integer dimensions", () => {
  assert.throws(() => normalizeShape([0]), RangeError);
  assert.throws(() => normalizeShape([-2]), RangeError);
  assert.throws(() => normalizeShape([1.5]), RangeError);
  assert.throws(() => normalizeShape("nope"), TypeError);
  assert.deepEqual([...normalizeShape([1, 2, 3])], [1, 2, 3]);
  assert.ok(Object.isFrozen(normalizeShape([1])));
});

test("request keys distinguish every request field", () => {
  const base = { modelVersion: "v1", shape: shape([2, 2]), layout: "row-major", dtype: "float32" };
  assert.equal(requestKey(base), requestKey({ ...base }));
  assert.notEqual(requestKey(base), requestKey({ ...base, modelVersion: "v2" }));
  assert.notEqual(requestKey(base), requestKey({ ...base, shape: shape([2, 3]) }));
  assert.notEqual(requestKey(base), requestKey({ ...base, layout: "tiled" }));
  assert.notEqual(requestKey(base), requestKey({ ...base, dtype: "float16" }));
});
