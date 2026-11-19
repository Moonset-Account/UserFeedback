/**
 * A numbered local stand-in for the injected system random source.
 *
 * It is deliberately independent of any real entropy: every initialization and
 * every read is recorded so tests can prove which requests received bytes and
 * which successful initialization those bytes came from.
 *
 * The byte payload of a read is deterministic: every byte equals the
 * provider-assigned `initializationId` of the initialization that produced it
 * (mod 256). That makes the origin of handed-out bytes observable in the
 * returned credential material itself, not only in the bookkeeping arrays.
 *
 * Modes
 * -----
 * - `auto: true` (default): `initialize()` resolves on the next microtask.
 * - `auto: false`: `initialize()` stays pending until the test calls
 *   `resolveInitialization(index)` / `rejectInitialization(index, error)`.
 *
 * Records
 * -------
 * - `initializations`: one entry per `initialize()` call, in call order:
 *   { index, generation, attempt, state, initializationId, reads }
 * - `reads`: one entry per `randomBytes()` call:
 *   { initializationId, generation, attempt, requestId, size, label, bytes }
 */

export function createNumberedRandomSource({ auto = true, labelPrefix = 'init' } = {}) {
  /** @type {Array<object>} */
  const initializations = [];
  /** @type {Array<object>} */
  const reads = [];

  let readFailure = null; // (context) => Error | null

  function initialize(context = {}) {
    const index = initializations.length;
    const record = {
      index,
      label: `${labelPrefix}-${index + 1}`,
      generation: context.generation ?? null,
      attempt: context.attempt ?? null,
      state: 'pending',
      initializationId: null,
      reads: [],
      resolvePromise: null,
      rejectPromise: null,
      promise: null,
    };

    if (auto) {
      record.promise = Promise.resolve();
      record.state = 'resolved-pending-microtask';
    } else {
      record.promise = new Promise((resolve, reject) => {
        record.resolvePromise = resolve;
        record.rejectPromise = reject;
      });
    }

    initializations.push(record);
    return record.promise;
  }

  function randomBytes(size, context = {}) {
    const failure = typeof readFailure === 'function' ? readFailure(context) : null;
    if (failure) {
      throw failure;
    }
    const initializationId = context.initializationId ?? null;
    const bytes = new Uint8Array(size);
    bytes.fill((initializationId ?? 0) % 256);

    const record = {
      initializationId,
      generation: context.generation ?? null,
      attempt: context.attempt ?? null,
      requestId: context.requestId ?? null,
      label: context.label ?? null,
      size,
      bytes: Uint8Array.from(bytes),
    };
    reads.push(record);

    const initialization = initializations.find(
      (entry) =>
        entry.generation === record.generation && entry.attempt === record.attempt,
    );
    if (initialization) initialization.reads.push(record);

    return bytes;
  }

  function resolveInitialization(index) {
    const record = initializations[index];
    if (!record) throw new RangeError(`no initialization at index ${index}`);
    if (record.state === 'rejected') {
      throw new Error(`initialization ${index} already rejected`);
    }
    record.state = 'resolved';
    // Only manual records have deferred resolvers; auto records settle through
    // their already-resolved promise.
    record.resolvePromise?.();
  }

  function rejectInitialization(index, error = new Error(`initialization ${index} failed`)) {
    const record = initializations[index];
    if (!record) throw new RangeError(`no initialization at index ${index}`);
    if (record.state === 'resolved') {
      throw new Error(`initialization ${index} already resolved`);
    }
    record.state = 'rejected';
    record.rejectPromise?.(error);
  }

  return {
    // Source contract
    initialize,
    randomBytes,

    // Bookkeeping
    initializations,
    reads,

    get initializationCount() {
      return initializations.length;
    },

    get readCount() {
      return reads.length;
    },

    initializationAt(index) {
      return initializations[index] ?? null;
    },

    /** Reads attributed to a given provider initialization id. */
    readsFrom(initializationId) {
      return reads.filter((read) => read.initializationId === initializationId);
    },

    /** Reads attributed to a generation number. */
    readsForGeneration(generation) {
      return reads.filter((read) => read.generation === generation);
    },

    /** Every initialization call made for a generation number. */
    initializationsFor(generation) {
      return initializations.filter((entry) => entry.generation === generation);
    },

    /** Set a predicate returning an Error to make matching reads fail. */
    failReadWhen(predicate) {
      readFailure = predicate;
    },

    clearReadFailures() {
      readFailure = null;
    },

    // Manual-mode controls
    resolveInitialization,
    rejectInitialization,
  };
}
