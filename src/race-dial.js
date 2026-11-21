import { AddressSelector } from './address-selector.js';

const MAX_CONCURRENT = 2;

// Real clock used when the caller does not inject one.
const defaultClock = {
  now: () => Date.now(),
  setTimeout: (fn, delay) => setTimeout(fn, delay),
  clearTimeout: (handle) => clearTimeout(handle),
};

// Close a connection without ever throwing out of the cleanup path.
function closeQuietly(connection) {
  if (connection == null || typeof connection.close !== 'function') return;
  try {
    Promise.resolve(connection.close()).catch(() => {
      // Closing an unwanted connection is best-effort; a close failure must
      // never change the outcome of the race.
    });
  } catch {
    // Synchronous throw from close(): likewise ignore.
  }
}

/**
 * Race dialing over ordered IPv6/IPv4 candidate lists.
 *
 * Scheduling rules:
 *   - The first address is dialed immediately (at logical time t0).
 *   - While there is no winner and candidates remain, at most one more dial is
 *     opened every `delayMs` milliseconds.
 *   - At most MAX_CONCURRENT (2) attempts are in flight at any moment.
 *   - A failure that frees a slot immediately starts the next candidate, and
 *     the next timed dial is re-timed from this new dialing instant.
 *   - At the same logical instant, already-returned dial results are processed
 *     before a due timer is acted upon.
 *
 * Resolution rules:
 *   - The first successful attempt wins; among successes whose notifications
 *     arrive in the same settling batch, the one whose dial started earlier
 *     (lower sequence number) wins.
 *   - Every other attempt is canceled; a connection whose success is observed
 *     only after cancellation/finalization is closed instead of being returned.
 *   - If every address fails, the promise rejects with an AllAddressesFailedError
 *     whose `failures` are ordered by dial start order.
 *
 * @param {object}   options
 * @param {string[]} options.ipv6        Ordered IPv6 candidates.
 * @param {string[]} options.ipv4        Ordered IPv4 candidates.
 * @param {number}   options.delayMs     D, milliseconds between timed dials.
 * @param {Function} options.dial        (address, family, signal) => Promise<connection>
 * @param {object}   [options.clock]     Injectable { now, setTimeout, clearTimeout }.
 * @returns {Promise<{address:string, family:string, connection:object, dialedAt:number}>}
 */
export function raceDial({ ipv6 = [], ipv4 = [], delayMs, dial, clock = defaultClock }) {
  if (!Number.isFinite(delayMs) || delayMs < 0) {
    throw new TypeError('delayMs must be a non-negative finite number');
  }
  if (typeof dial !== 'function') {
    throw new TypeError('dial must be a function (address, family, signal) => Promise');
  }
  if (clock == null || typeof clock.now !== 'function'
    || typeof clock.setTimeout !== 'function'
    || typeof clock.clearTimeout !== 'function') {
    throw new TypeError('clock must provide now(), setTimeout() and clearTimeout()');
  }

  const selector = new AddressSelector(ipv6, ipv4);
  const failures = []; // appended as rejections arrive; sorted by sequence later
  const active = new Map(); // sequence -> attempt record
  let sequence = 0;
  let timerHandle = null;
  // Independent scheduling generations. Every (re)schedule gets a fresh value;
  // a queued timer callback whose generation is stale was superseded (typically
  // by a failure-driven immediate redial) and must not open another dial.
  // This is deliberately separate from timerHandle: consuming a timer must not
  // invalidate the generation captured by the next scheduleTimer() call.
  let timerGeneration = 0;
  let winner = null;
  let settled = false;
  let finalizeQueued = false;

  // Serialize every state transition (settlement reactions and timer fires) so
  // the semantics are identical regardless of how the injected clock orders
  // its callbacks. Every job is synchronous once it holds the lock.
  let chain = Promise.resolve();
  const locked = (job) => {
    chain = chain.then(job, job);
    return chain;
  };

  let resolveResult;
  let rejectResult;
  const result = new Promise((resolve, reject) => {
    resolveResult = resolve;
    rejectResult = reject;
  });

  function clearTimer() {
    if (timerHandle !== null) {
      clock.clearTimeout(timerHandle);
      timerHandle = null;
    }
    // Invalidate any callback the underlying clock may already have queued.
    timerGeneration += 1;
  }

  function scheduleTimer() {
    if (settled || winner || !selector.hasNext()) return;
    clearTimer();
    timerGeneration += 1; // fresh generation for this schedule
    const generation = timerGeneration;
    timerHandle = clock.setTimeout(() => {
      // Enter through a microtask: dial-promise reactions already queued at
      // this instant run first ("same instant: results before timer"), then the
      // timer action is re-validated under the serialization lock.
      void locked(() => {
        if (generation !== timerGeneration) return; // superseded timer
        handleTimer();
      });
    }, delayMs);
  }

  function handleTimer() {
    if (settled || winner || !selector.hasNext()) return;
    if (active.size < MAX_CONCURRENT) {
      startAttempt();
    }
    // Whether or not a slot was free, this timer generation is consumed.
    timerHandle = null;
    // Slots full: the tick is skipped (no dial beyond the concurrency cap).
    // Re-arm from the current instant so the following timed dial remains at
    // most one per D ms; any failure in between supersedes this timer and
    // re-times it from that failure's replacement dial.
    if (!settled && !winner && selector.hasNext()) {
      scheduleTimer();
    }
  }

  function startAttempt() {
    const candidate = selector.next();
    if (candidate === null) return null;

    const seq = sequence++;
    const controller = new AbortController();
    const attempt = {
      seq,
      address: candidate.address,
      family: candidate.family,
      familyIndex: candidate.index,
      dialedAt: clock.now(),
      signal: controller.signal,
      cancel: () => controller.abort(),
      canceled: false,
    };
    active.set(seq, attempt);

    // A synchronous throw from `dial` is treated exactly like an asynchronous
    // rejection of that single attempt.
    let attemptPromise;
    try {
      attemptPromise = Promise.resolve(
        dial(candidate.address, candidate.family, controller.signal),
      );
    } catch (err) {
      attemptPromise = Promise.reject(err);
    }

    attemptPromise.then(
      (connection) => locked(() => handleFulfilled(attempt, connection)),
      (error) => locked(() => handleRejected(attempt, error)),
    );
    return attempt;
  }

  function handleFulfilled(attempt, connection) {
    if (settled) {
      // The race already finalized: never leak the extra connection.
      closeQuietly(connection);
      return;
    }

    if (winner === null) {
      winner = { attempt, connection };
    } else if (attempt.seq < winner.attempt.seq) {
      // A success from an earlier-dialed attempt arriving in the same settling
      // batch takes over; the demoted connection is closed.
      closeQuietly(winner.connection);
      winner = { attempt, connection };
    } else {
      // Later-dialed success while a winner already exists: unwanted.
      attempt.canceled = true;
      closeQuietly(connection);
      maybeFinish();
      return;
    }

    maybeFinish();
  }

  function handleRejected(attempt, error) {
    if (settled || winner) return; // failures after a win are irrelevant
    if (active.get(attempt.seq) !== attempt) return;

    active.delete(attempt.seq);
    failures.push({
      sequence: attempt.seq,
      address: attempt.address,
      family: attempt.family,
      dialedAt: attempt.dialedAt,
      error,
    });

    // If this was the last in-flight attempt and no candidate remains, the
    // whole race has failed — decide this before starting any replacement.
    if (!selector.hasNext() && active.size === 0) {
      finishAllFailed();
      return;
    }

    if (selector.hasNext()) {
      // Free slot: dial the next candidate immediately, and re-time the next
      // timed dial from this instant.
      startAttempt();
      scheduleTimer();
      return;
    }

    // Candidates exhausted but other attempts are still in flight: no further
    // timed dial can ever be needed.
    clearTimer();
  }

  function maybeFinish() {
    if (settled || winner === null) return;

    // Cancel every in-flight attempt other than the current winner.
    for (const attempt of active.values()) {
      if (attempt.seq === winner.attempt.seq || attempt.canceled) continue;
      attempt.canceled = true;
      attempt.cancel();
    }

    // Defer finalization by one locked job. Settlement reactions already
    // queued in this batch run first, so an earlier-dialed same-batch success
    // can still take over; reactions arriving afterwards see `settled` and are
    // closed as post-cancellation successes.
    if (!finalizeQueued) {
      finalizeQueued = true;
      queueMicrotask(() => locked(finalize));
    }
  }

  function finalize() {
    if (settled) return;
    finalizeQueued = false;
    for (const attempt of active.values()) {
      if (attempt.seq === winner.attempt.seq || attempt.canceled) continue;
      attempt.canceled = true;
      attempt.cancel();
    }
    const won = winner;
    settled = true;
    winner = null;
    active.clear();
    clearTimer();
    resolveResult({
      address: won.attempt.address,
      family: won.attempt.family,
      connection: won.connection,
      dialedAt: won.attempt.dialedAt,
    });
  }

  function finishAllFailed() {
    if (settled) return;
    settled = true;
    clearTimer();
    active.clear();
    // Dial start order = strictly increasing sequence numbers.
    const ordered = [...failures].sort((a, b) => a.sequence - b.sequence);
    rejectResult(new AllAddressesFailedError(ordered));
  }

  if (!selector.hasNext()) {
    finishAllFailed();
    return result;
  }

  // First address dials immediately; the timer for the next one starts now.
  startAttempt();
  if (selector.hasNext()) {
    scheduleTimer();
  }
  return result;
}

export class AllAddressesFailedError extends Error {
  constructor(failures) {
    super(`All ${failures.length} address attempt(s) failed`);
    this.name = 'AllAddressesFailedError';
    this.failures = failures.map(({ sequence, address, family, dialedAt, error }) => ({
      sequence,
      address,
      family,
      dialedAt,
      error,
    }));
  }
}
