// Shape guards for compiled inference plans.
//
// A guard describes *which requests* a compiled plan may serve:
//   - per-dimension inclusive ranges (dims[i] = { min, max })
//   - inter-dimension equality pairs      (equal: [[a, b], ...])
//   - inter-dimension divisibility pairs  (divisible: [{ numerator, denominator }, ...],
//                                          meaning shape[numerator] % shape[denominator] === 0)
//   - exact layout / data-type requirements
//
// Reuse requires every clause to pass. Comparing only per-dimension maxima is
// explicitly insufficient: two dimensions can each lie inside their allowed
// ranges while an equality/divisibility clause between them is violated.

const isInt = (value) => Number.isInteger(value);

/**
 * Validate and normalize a shape vector.
 * Every dimension must be a positive integer.
 * @param {Array<number>} shape
 * @returns {Array<number>} a frozen copy of the shape
 */
export function normalizeShape(shape) {
  if (!Array.isArray(shape)) {
    throw new TypeError("shape must be an array of positive integers");
  }
  const out = [];
  for (const value of shape) {
    if (!isInt(value) || value <= 0) {
      throw new RangeError(`shape dimensions must be positive integers, got ${String(value)}`);
    }
    out.push(value);
  }
  return Object.freeze(out);
}

function checkIndex(index, rank, label) {
  if (!isInt(index) || index < 0 || index >= rank) {
    throw new RangeError(`${label} dimension index ${String(index)} is out of range for rank ${rank}`);
  }
}

/**
 * Validate and normalize a guard for a given rank.
 * The returned guard is deeply frozen and safe to store on a cache entry.
 * @param {object} guard
 * @param {number} rank number of shape dimensions
 */
export function normalizeGuard(guard, rank) {
  if (!isInt(rank) || rank < 0) {
    throw new RangeError("rank must be a non-negative integer");
  }
  if (guard === null || typeof guard !== "object" || Array.isArray(guard)) {
    throw new TypeError("guard must be an object");
  }

  const dims = new Array(rank);
  if (guard.dims !== undefined) {
    if (!Array.isArray(guard.dims) || guard.dims.length !== rank) {
      throw new RangeError(`guard.dims must have exactly ${rank} entries`);
    }
    for (let i = 0; i < rank; i += 1) {
      const range = guard.dims[i];
      if (range === null || typeof range !== "object") {
        throw new TypeError(`guard.dims[${i}] must be { min, max }`);
      }
      const { min, max } = range;
      if (!isInt(min) || !isInt(max) || min <= 0 || max < min) {
        throw new RangeError(`guard.dims[${i}] must satisfy 0 < min <= max`);
      }
      dims[i] = Object.freeze({ min, max });
    }
  } else {
    for (let i = 0; i < rank; i += 1) {
      dims[i] = Object.freeze({ min: 1, max: Number.MAX_SAFE_INTEGER });
    }
  }

  const equal = [];
  if (guard.equal !== undefined) {
    if (!Array.isArray(guard.equal)) {
      throw new TypeError("guard.equal must be an array of index pairs");
    }
    for (const pair of guard.equal) {
      if (!Array.isArray(pair) || pair.length !== 2) {
        throw new TypeError("guard.equal entries must be [indexA, indexB]");
      }
      const [a, b] = pair;
      checkIndex(a, rank, "guard.equal");
      checkIndex(b, rank, "guard.equal");
      equal.push(Object.freeze([a, b]));
    }
  }

  const divisible = [];
  if (guard.divisible !== undefined) {
    if (!Array.isArray(guard.divisible)) {
      throw new TypeError("guard.divisible must be an array of { numerator, denominator }");
    }
    for (const clause of guard.divisible) {
      if (clause === null || typeof clause !== "object") {
        throw new TypeError("guard.divisible entries must be { numerator, denominator }");
      }
      const { numerator, denominator } = clause;
      checkIndex(numerator, rank, "guard.divisible numerator");
      checkIndex(denominator, rank, "guard.divisible denominator");
      divisible.push(Object.freeze({ numerator, denominator }));
    }
  }

  const normalized = {
    dims: Object.freeze(dims),
    equal: Object.freeze(equal),
    divisible: Object.freeze(divisible),
    layout: guard.layout === undefined ? null : String(guard.layout),
    dtype: guard.dtype === undefined ? null : String(guard.dtype),
  };
  return Object.freeze(normalized);
}

/**
 * Full guard check. Returns true only when version-independent shape/layout/dtype
 * clauses all pass. Model-version equality is checked separately by the cache.
 * @param {object} guard a normalized guard
 * @param {Array<number>} shape a normalized shape
 * @param {object} request
 */
export function guardMatches(guard, shape, request) {
  const rank = shape.length;
  if (guard.dims.length !== rank) {
    return false;
  }

  // 1. Per-dimension ranges.
  for (let i = 0; i < rank; i += 1) {
    const { min, max } = guard.dims[i];
    if (shape[i] < min || shape[i] > max) {
      return false;
    }
  }

  // 2. Inter-dimension equality. This is the clause a max-only comparison misses.
  for (const [a, b] of guard.equal) {
    if (shape[a] !== shape[b]) {
      return false;
    }
  }

  // 3. Inter-dimension divisibility: shape[numerator] is a multiple of shape[denominator].
  for (const { numerator, denominator } of guard.divisible) {
    if (shape[numerator] % shape[denominator] !== 0) {
      return false;
    }
  }

  // 4. Layout / data type.
  if (guard.layout !== null && request.layout !== guard.layout) {
    return false;
  }
  if (guard.dtype !== null && request.dtype !== guard.dtype) {
    return false;
  }

  return true;
}

/**
 * Stable serialization used as the coalescing key for *identical* requests.
 * Only semantically identical requests may be merged into one compilation.
 */
export function requestKey(request) {
  return JSON.stringify({
    v: request.modelVersion,
    s: request.shape,
    l: request.layout ?? null,
    d: request.dtype ?? null,
  });
}
