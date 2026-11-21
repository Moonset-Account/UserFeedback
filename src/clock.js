/**
 * Clock abstractions used by the load generator.
 *
 * Both clocks implement the same surface:
 *   - now(): current time in milliseconds
 *   - scheduleAt(time, fn, priority?): schedule fn at an absolute time;
 *     lower priority numbers fire first among events at the same instant
 *   - setTimeout(fn, delayMs, priority?): relative-time convenience wrapper
 *
 * Event ordering at the same timestamp is (time, priority, insertion sequence).
 * The load generator schedules completion callbacks with priority 0 and
 * arrivals with priority 1, so a slot release always happens before a new
 * arrival sharing the same timestamp: the earliest queued item is picked up
 * first, and only then do the new arrivals get processed.
 */

export class VirtualClock {
  #now = 0;
  #entries = [];
  #seq = 0;

  now() {
    return this.#now;
  }

  scheduleAt(time, fn, priority = 0) {
    if (!Number.isFinite(time) || time < this.#now) {
      throw new RangeError(`scheduleAt: time ${time} is before current time ${this.#now}`);
    }
    const entry = { time, priority, seq: this.#seq++, fn };
    this.#entries.push(entry);
    this.#entries.sort((a, b) => a.time - b.time || a.priority - b.priority || a.seq - b.seq);
    return {
      cancel: () => {
        const idx = this.#entries.indexOf(entry);
        if (idx !== -1) this.#entries.splice(idx, 1);
      },
    };
  }

  setTimeout(fn, delayMs, priority = 0) {
    if (!Number.isFinite(delayMs) || delayMs < 0) {
      throw new RangeError(`setTimeout: invalid delay ${delayMs}`);
    }
    return this.scheduleAt(this.#now + delayMs, fn, priority);
  }

  /**
   * Pump every scheduled event in chronological order. New events scheduled
   * by callbacks are picked up in the same run. Returns when no entries
   * remain (or the next entry is later than `until`).
   */
  run(until = Infinity) {
    while (this.#entries.length > 0) {
      const next = this.#entries[0];
      if (next.time > until) break;
      this.#entries.shift();
      this.#now = next.time;
      next.fn();
    }
    if (Number.isFinite(until) && this.#now < until) this.#now = until;
    return this.#now;
  }

  pendingCount() {
    return this.#entries.length;
  }
}

export class RealClock {
  #entries = new Map();
  #seq = 0;
  #timer = null;

  now() {
    return performance.now();
  }

  scheduleAt(time, fn, priority = 0) {
    const id = ++this.#seq;
    this.#entries.set(id, { id, time, priority, seq: id, fn });
    this.#pump();
    return {
      cancel: () => {
        this.#entries.delete(id);
      },
    };
  }

  setTimeout(fn, delayMs, priority = 0) {
    return this.scheduleAt(this.now() + delayMs, fn, priority);
  }

  #pump() {
    if (this.#timer !== null) {
      clearTimeout(this.#timer);
      this.#timer = null;
    }
    if (this.#entries.size === 0) return;
    const nextAt = Math.min(...[...this.#entries.values()].map((e) => e.time));
    const delay = Math.max(0, nextAt - this.now());
    this.#timer = setTimeout(() => this.#tick(), delay);
  }

  #tick() {
    this.#timer = null;
    const now = this.now();
    const due = [...this.#entries.values()]
      .filter((e) => e.time <= now)
      .sort((a, b) => a.priority - b.priority || a.seq - b.seq);
    for (const entry of due) {
      if (!this.#entries.has(entry.id)) continue; // cancelled while waiting
      this.#entries.delete(entry.id);
      entry.fn();
    }
    this.#pump();
  }

  pendingCount() {
    return this.#entries.size;
  }

  /** Resolve once the event queue has drained. */
  async idle(pollIntervalMs = 2) {
    while (this.#entries.size > 0) {
      await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
    }
  }
}
