import { CoordinatorError } from './errors.js';

const ALL_DIGITS = /^\d+$/;

/**
 * Deterministic total order for experiment ids ("试验编号").
 *
 * - Two all-digit ids compare numerically (`2` before `10`).
 * - An all-digit id sorts before a non-numeric id.
 * - Otherwise code-unit order is used (stable across machines and locales).
 *
 * @param {string} a
 * @param {string} b
 * @returns {-1 | 0 | 1}
 */
export function compareExperimentIds(a, b) {
  const aNumeric = ALL_DIGITS.test(a) ? BigInt(a) : null;
  const bNumeric = ALL_DIGITS.test(b) ? BigInt(b) : null;

  if (aNumeric !== null && bNumeric !== null) {
    if (aNumeric < bNumeric) return -1;
    if (aNumeric > bNumeric) return 1;
    // Numerically equal (e.g. "01" vs "1"): fall through to a stable
    // code-unit comparison so the order is still total and deterministic.
  } else if (aNumeric !== null) {
    return -1;
  } else if (bNumeric !== null) {
    return 1;
  }

  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

/**
 * Promotion quota: floor(n / 2), but always at least one when n > 0.
 *
 * @param {number} n number of eligible, ranked experiments
 * @returns {number}
 */
export function retentionCount(n) {
  if (!Number.isSafeInteger(n) || n < 0) {
    throw new TypeError('retentionCount expects a non-negative safe integer');
  }
  if (n === 0) return 0;
  return Math.max(1, Math.floor(n / 2));
}

function bigintGcd(a, b) {
  let x = a < 0n ? -a : a;
  let y = b < 0n ? -b : b;
  while (y !== 0n) {
    const t = x % y;
    x = y;
    y = t;
  }
  return x;
}

/**
 * Reduced exact representation of correct/samples, e.g. "3/4".
 * Uses BigInt so no floating point rounding can create or hide a tie.
 */
export function ratioString(correct, samples) {
  if (!Number.isSafeInteger(correct) || correct < 0) {
    throw new TypeError('correct must be a non-negative safe integer');
  }
  if (!Number.isSafeInteger(samples) || samples <= 0) {
    throw new TypeError('samples must be a positive safe integer');
  }
  const c = BigInt(correct);
  const s = BigInt(samples);
  const g = bigintGcd(c, s);
  return `${c / g}/${s / g}`;
}

/**
 * Exact comparison of two aggregated results, best score first.
 * Ties are broken by experiment id ascending. No floating point math.
 */
export function compareScoreEntries(a, b) {
  const left = BigInt(a.correct) * BigInt(b.samples);
  const right = BigInt(b.correct) * BigInt(a.samples);
  if (left !== right) return left > right ? -1 : 1;
  return compareExperimentIds(a.experimentId, b.experimentId);
}

/**
 * Aggregate per-fold counts into one score:
 * sum(correct) / sum(samples) -- deliberately sample-weighted, never a
 * macro average of per-fold accuracies.
 *
 * @param {{name: string, correct: number, samples: number}[]} folds
 */
export function aggregateFolds(folds) {
  let correct = 0;
  let samples = 0;
  for (const fold of folds) {
    if (!Number.isSafeInteger(fold.correct) || fold.correct < 0) {
      throw new CoordinatorError(
        'INVALID_COUNT',
        `fold "${fold.name}" has an invalid correct count`,
        { fold: fold.name, correct: fold.correct },
      );
    }
    if (!Number.isSafeInteger(fold.samples) || fold.samples <= 0) {
      throw new CoordinatorError(
        'INVALID_COUNT',
        `fold "${fold.name}" has an invalid sample count`,
        { fold: fold.name, samples: fold.samples },
      );
    }
    if (fold.correct > fold.samples) {
      throw new CoordinatorError(
        'INVALID_COUNT',
        `fold "${fold.name}" has more correct answers than samples`,
        { fold: fold.name, correct: fold.correct, samples: fold.samples },
      );
    }
    correct += fold.correct;
    samples += fold.samples;
    if (!Number.isSafeInteger(correct) || !Number.isSafeInteger(samples)) {
      throw new CoordinatorError(
        'COUNT_OVERFLOW',
        'aggregated counts exceed Number.MAX_SAFE_INTEGER',
        { correct, samples },
      );
    }
  }
  if (samples === 0) {
    throw new CoordinatorError(
      'INVALID_COUNT',
      'a submission must cover at least one sample',
    );
  }
  return {
    correct,
    samples,
    score: correct / samples,
    scoreRatio: ratioString(correct, samples),
  };
}

/**
 * Deep-freeze a JSON-shaped value (objects/arrays). Maps and Dates are not
 * expected in snapshots; this helper intentionally only walks plain data.
 */
export function deepFreeze(value, seen = new WeakSet()) {
  if (value === null || typeof value !== 'object') return value;
  if (seen.has(value)) return value;
  seen.add(value);
  for (const key of Object.keys(value)) deepFreeze(value[key], seen);
  return Object.freeze(value);
}

/** Copy a plain JSON-shaped value so callers cannot mutate coordinator state. */
export function cloneJson(value) {
  if (value === null || typeof value !== 'object') return value;
  if (Array.isArray(value)) return value.map(cloneJson);
  const out = {};
  for (const key of Object.keys(value)) out[key] = cloneJson(value[key]);
  return out;
}

export function isNonEmptyString(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

/**
 * Validate and canonicalise one attempt's per-fold report against the frozen
 * fold list. Order in the input does not matter; output follows the frozen
 * fold order.
 *
 * @param {unknown} input
 * @param {string[]} foldNames frozen fold names for this round
 * @param {Map<string, number | null>} expectedSamples declared or already
 *   established sample count per fold
 */
export function normalizeFoldResults(input, foldNames, expectedSamples) {
  if (!Array.isArray(input)) {
    throw new CoordinatorError(
      'FOLD_SET_MISMATCH',
      'folds must be an array of { name, correct, samples } objects',
      { expected: [...foldNames] },
    );
  }

  const byName = new Map();
  for (const raw of input) {
    if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
      throw new CoordinatorError(
        'FOLD_SET_MISMATCH',
        'every fold result must be an object',
        { expected: [...foldNames] },
      );
    }
    const { name, correct, samples } = raw;
    if (!isNonEmptyString(name) || !foldNames.includes(name)) {
      throw new CoordinatorError(
        'FOLD_SET_MISMATCH',
        `fold ${JSON.stringify(name)} is not part of the frozen fold list`,
        { expected: [...foldNames], received: name },
      );
    }
    if (byName.has(name)) {
      throw new CoordinatorError(
        'FOLD_SET_MISMATCH',
        `fold "${name}" was reported more than once`,
        { fold: name },
      );
    }
    if (!Number.isSafeInteger(correct) || correct < 0 || !Number.isSafeInteger(samples) || samples <= 0) {
      throw new CoordinatorError(
        'INVALID_COUNT',
        `fold "${name}" needs a non-negative integer correct and a positive integer samples`,
        { fold: name, correct, samples },
      );
    }
    if (correct > samples) {
      throw new CoordinatorError(
        'INVALID_COUNT',
        `fold "${name}" has more correct answers than samples`,
        { fold: name, correct, samples },
      );
    }
    const expected = expectedSamples.get(name);
    if (expected !== null && expected !== undefined && expected !== samples) {
      throw new CoordinatorError(
        'FOLD_SAMPLE_MISMATCH',
        `fold "${name}" reports ${samples} samples but this round fixed ${expected}`,
        { fold: name, expected, received: samples },
      );
    }
    byName.set(name, { name, correct, samples });
  }

  const missing = foldNames.filter((name) => !byName.has(name));
  if (missing.length > 0) {
    throw new CoordinatorError(
      'FOLD_SET_MISMATCH',
      `missing fold results: ${missing.join(', ')}`,
      { expected: [...foldNames], missing },
    );
  }

  return foldNames.map((name) => ({ ...byName.get(name) }));
}

/**
 * Validate and copy a checkpoint reference.
 *
 * A checkpoint always carries:
 *  - `path`   where the bytes live,
 *  - `step`   completed training steps (observed state, not an instruction),
 *  - `digest` content identity of the bytes.
 *
 * The digest is required because it is what makes "the superseded attempt's
 * checkpoint is disqualified" enforceable: a renamed copy of the same bytes is
 * still the same disqualified checkpoint.
 */
export function normalizeCheckpoint(input, expectedStep) {
  if (input === null || typeof input !== 'object' || Array.isArray(input)) {
    throw new CoordinatorError(
      'INVALID_CHECKPOINT',
      'checkpoint must be an object { path, step, digest }',
    );
  }
  if (!isNonEmptyString(input.path)) {
    throw new CoordinatorError(
      'INVALID_CHECKPOINT',
      'checkpoint.path must be a non-empty string',
    );
  }
  if (!Number.isSafeInteger(input.step) || input.step < 0) {
    throw new CoordinatorError(
      'INVALID_CHECKPOINT',
      'checkpoint.step must be a non-negative safe integer',
      { step: input.step },
    );
  }
  if (input.step !== expectedStep) {
    throw new CoordinatorError(
      'CHECKPOINT_STEP_MISMATCH',
      `checkpoint is at step ${input.step} but this round trains to ${expectedStep}`,
      { expected: expectedStep, received: input.step },
    );
  }
  if (!isNonEmptyString(input.digest)) {
    throw new CoordinatorError(
      'INVALID_CHECKPOINT',
      'checkpoint.digest must be a non-empty content digest',
    );
  }
  return { path: input.path, step: input.step, digest: input.digest };
}
