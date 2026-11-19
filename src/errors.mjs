/**
 * Errors raised by the random-byte provider layer.
 *
 * All provider-specific errors carry a stable `code` so callers can branch on
 * the failure without matching English messages.
 */

export class RandomBytesProviderError extends Error {
  constructor(message, code, options) {
    super(message, options);
    this.name = new.target.name;
    this.code = code;
  }
}

export class RandomBytesProviderClosedError extends RandomBytesProviderError {
  constructor() {
    super('The random byte provider has been closed', 'ERR_RANDOM_BYTES_CLOSED');
  }
}

/**
 * Raised when the injected random source fails to initialize. Every waiter of
 * the affected generation receives the same error instance.
 */
export class RandomBytesInitializationError extends RandomBytesProviderError {
  constructor({ generation, attempt, cause } = {}) {
    super(
      `Random source initialization failed (generation ${generation}, attempt ${attempt})`,
      'ERR_RANDOM_BYTES_INIT',
      cause === undefined ? undefined : { cause },
    );
    this.generation = generation;
    this.attempt = attempt;
  }
}

/**
 * Raised for the single waiter whose request was cancelled through its
 * AbortSignal. `name` follows the DOM convention so generic abort handling
 * keeps working.
 */
export class RandomBytesRequestAbortedError extends RandomBytesProviderError {
  constructor(reason) {
    super('Random byte request was aborted', 'ABORT_ERR', {
      cause: reason instanceof Error ? reason : undefined,
    });
    this.name = 'AbortError';
    this.reason = reason;
  }
}

/**
 * Raised when a successfully initialized source fails to produce bytes for a
 * single request. The generation stays ready; only that request is rejected.
 */
export class RandomBytesReadError extends RandomBytesProviderError {
  constructor({ generation, requestId, cause } = {}) {
    super(
      `Random source failed to produce bytes (generation ${generation}, request ${requestId})`,
      'ERR_RANDOM_BYTES_READ',
      cause === undefined ? undefined : { cause },
    );
    this.generation = generation;
    this.requestId = requestId;
  }
}

export function createAbortError(reason) {
  return new RandomBytesRequestAbortedError(reason);
}

/**
 * Raised for waiters that were still pending when their generation was
 * superseded (e.g. a working copy was produced). Their bytes would have come
 * from an initialization belonging to the parent process, so they are not
 * served from the old generation.
 */
export class RandomBytesGenerationSupersededError extends RandomBytesProviderError {
  constructor({ previousGeneration, generation, reason } = {}) {
    super(
      `Random bytes request belongs to superseded generation ${previousGeneration}; ` +
        `generation ${generation} is now current` +
        (reason ? ` (${reason})` : ''),
      'ERR_RANDOM_BYTES_GENERATION_SUPERSEDED',
    );
    this.previousGeneration = previousGeneration;
    this.generation = generation;
    this.reason = reason;
  }
}
