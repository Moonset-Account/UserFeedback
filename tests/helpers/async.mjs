import { setImmediate as tick } from 'node:timers/promises';

/**
 * Attach handlers immediately so a rejection can never be reported as
 * unhandled while the test is still setting up other actors. Returns an
 * outcome object instead of a bare promise.
 */
export function track(promise) {
  return promise.then(
    (value) => ({ ok: true, value }),
    (error) => ({ ok: false, error }),
  );
}

/** Let queued microtasks and the next macrotask turn run. */
export async function flush() {
  await tick();
}

/**
 * Await a tracked outcome, failing fast with a descriptive error instead of
 * hanging the test runner when the promise never settles.
 */
export async function settle(outcomePromise, description = 'request', timeoutMs = 250) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(`${description} did not settle within ${timeoutMs}ms`)),
      timeoutMs,
    );
  });
  try {
    return await Promise.race([outcomePromise, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

/** Fill helper: the byte value the numbered source uses for an init id. */
export function byteFor(initializationId) {
  return initializationId % 256;
}
