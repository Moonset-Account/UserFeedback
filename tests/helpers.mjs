/**
 * Test-only fakes: a controllable connect callback whose every invocation
 * returns a deferred promise, and fake transport objects that record the
 * real close() calls the pool makes against them.
 */

let connCounter = 0;

export class FakeConnection {
  constructor(address) {
    this.id = ++connCounter;
    this.address = address;
    this.closed = false;
    this.closeCount = 0;
  }

  close() {
    this.closeCount += 1;
    this.closed = true;
  }
}

export function controllableConnect({ failOnResolve = false } = {}) {
  const calls = [];
  const connect = (address, discovery) => new Promise((resolve, reject) => {
    calls.push({
      address,
      discovery,
      resolve(payload) {
        resolve(payload ?? new FakeConnection(address));
      },
      reject(err) {
        reject(err ?? new Error(`connect failed for ${address}`));
      },
    });
  });
  return { connect, calls, failOnResolve };
}

/** A connect that resolves on the microtask queue (fast path testing). */
export function immediateConnect() {
  const calls = [];
  const connect = async (address, discovery) => {
    const conn = new FakeConnection(address);
    calls.push({ address, discovery, connection: conn });
    return conn;
  };
  return { connect, calls };
}

export function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/**
 * The pool batches its pump into a microtask and dispatches user connect
 * callbacks inside another .then() chain, so a dial can take several
 * microtask turns to reach the fake's call list. Flush until the observed
 * condition holds or the round budget is exhausted.
 */
export async function flushMicrotasks(rounds = 10) {
  for (let i = 0; i < rounds; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await Promise.resolve();
  }
}

/**
 * Flush until `predicate()` returns true (checked after every microtask
 * round) or the safety budget runs out. Returns the final predicate value.
 */
export async function flushUntil(predicate, rounds = 50) {
  for (let i = 0; i < rounds; i += 1) {
    if (predicate()) return true;
    // eslint-disable-next-line no-await-in-loop
    await Promise.resolve();
  }
  return predicate();
}

export function assertRejects(promise, ErrorClass, messageIncludes) {
  return promise.then(
    () => {
      throw new Error(`expected rejection of ${ErrorClass?.name ?? 'error'} but promise resolved`);
    },
    (err) => {
      if (ErrorClass && !(err instanceof ErrorClass)) {
        throw new Error(`expected ${ErrorClass.name}, got ${err.constructor.name}: ${err.message}`);
      }
      if (messageIncludes && !String(err.message).includes(messageIncludes)) {
        throw new Error(`error message missing "${messageIncludes}": ${err.message}`);
      }
      return err;
    },
  );
}

export function genSnapshot(stats, name, generation) {
  const key = `${name}#${generation}`;
  const snap = stats.generations.find((g) => g.key === key);
  if (!snap) throw new Error(`no stats for generation ${key}`);
  return snap;
}

export function closesFor(stats, name, generation) {
  const key = `${name}#${generation}`;
  return stats.closeEvents.filter((e) => e.key === key);
}
