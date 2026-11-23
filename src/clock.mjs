/**
 * Controllable virtual clock.
 *
 * The pool only needs three operations from a clock:
 *   - now():      current epoch milliseconds
 *   - setTimeout / clearTimeout
 *
 * ManualClock keeps time as an integer millisecond value and stores timers
 * in an ordered heap-ish array. Nothing fires until the test explicitly
 * moves the clock, which makes deadline / cancellation interleavings fully
 * deterministic. A system-clock adapter is provided for real deployments.
 */
export class ManualClock {
  #now;
  #timers = new Set();
  #nextId = 1;

  constructor(initialNow = 0) {
    if (!Number.isInteger(initialNow) || initialNow < 0) {
      throw new TypeError('initialNow must be a non-negative integer');
    }
    this.#now = initialNow;
  }

  now() {
    return this.#now;
  }

  setTimeout(fn, delayMs, ...args) {
    if (typeof fn !== 'function') throw new TypeError('callback must be a function');
    const delay = Math.max(0, Number(delayMs) || 0);
    const timer = {
      id: this.#nextId++,
      dueAt: this.#now + delay,
      seq: this.#nextId,
      fn,
      args,
      cancelled: false,
    };
    this.#timers.add(timer);
    return timer;
  }

  clearTimeout(timer) {
    if (!timer) return;
    this.#timers.delete(timer);
    timer.cancelled = true;
  }

  /** Number of timers still scheduled (tests use this to spot leaks). */
  get pendingTimerCount() {
    return this.#timers.size;
  }

  /**
   * Advance the clock by ms and run every timer whose due time is reached,
   * in due-time order (insertion order breaks ties). Expired timers are
   * removed before firing so a timer that reschedules itself works.
   */
  advance(ms) {
    if (!Number.isInteger(ms) || ms < 0) {
      throw new TypeError('ms must be a non-negative integer');
    }
    const target = this.#now + ms;
    this.#now = target;
    for (;;) {
      let due = null;
      for (const t of this.#timers) {
        if (t.dueAt <= target && (due === null || t.dueAt < due.dueAt ||
          (t.dueAt === due.dueAt && t.seq < due.seq))) {
          due = t;
        }
      }
      if (due === null) break;
      this.#timers.delete(due);
      due.cancelled = false;
      due.fn(...due.args);
    }
    return target;
  }

  /**
   * Advance the clock and then drain the microtask queue.
   * Promise continuations (including the pool's internal pumps) run here,
   * so a single tick() reproduces "deadline fires, then everything settles".
   */
  async tick(ms) {
    this.advance(ms);
    // 1000 rounds is far beyond any microtask chain used by the pool.
    for (let i = 0; i < 1000; i++) {
      const lenBefore = this.#timers.size;
      // `await Promise.resolve()` yields once to the microtask queue.
      // eslint-disable-next-line no-await-in-loop
      await Promise.resolve();
      if (lenBefore === this.#timers.size) {
        // One extra round to make sure nothing new was queued at the tail.
        // eslint-disable-next-line no-await-in-loop
        await Promise.resolve();
        break;
      }
    }
  }
}

/** Adapter over the real Node.js event loop, same interface as ManualClock. */
export const systemClock = {
  now() {
    return Date.now();
  },
  setTimeout(fn, ms, ...args) {
    return setTimeout(fn, ms, ...args);
  },
  clearTimeout(timer) {
    clearTimeout(timer);
  },
};
