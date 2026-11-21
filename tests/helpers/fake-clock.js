// Deterministic manual clock: time only moves when a test calls advance().
//
// On every advance(), all timers whose due time is at or before the new
// current time are dispatched (in due-time, then registration order), and
// dispatching keeps going until no timer remains due. This matches real timers,
// which fire as soon as their delay has elapsed even across multiple clock
// observations. Timers created while dispatching with due <= now also fire,
// modeling a zero/overdue delay.
//
// Dialer timer callbacks hop onto the microtask queue themselves, so dial
// reactions queued before advance() are still processed before the timer
// action.
export class FakeClock {
  #now = 0;
  #timers = new Map();
  #id = 0;

  now() {
    return this.#now;
  }

  setTimeout(fn, delay) {
    const id = ++this.#id;
    this.#timers.set(id, { fn, due: this.#now + delay });
    return id;
  }

  clearTimeout(handle) {
    this.#timers.delete(handle);
  }

  pendingTimers() {
    return this.#timers.size;
  }

  advance(ms) {
    this.#now += ms;
    // Bound the loop defensively; each iteration removes at least one timer.
    for (let guard = 0; guard < 10_000; guard += 1) {
      const due = [...this.#timers.entries()]
        .filter(([, t]) => t.due <= this.#now)
        .sort((a, b) => (a[1].due - b[1].due) || (a[0] - b[0]));
      if (due.length === 0) return;
      for (const [id, timer] of due) {
        if (!this.#timers.has(id)) continue; // cleared while dispatching
        this.#timers.delete(id);
        timer.fn();
      }
    }
    throw new Error('FakeClock timer dispatch loop did not converge');
  }
}

// Flush all currently queued microtasks (promise reactions, queueMicrotask).
// A generous fixed turn count drains even deeply chained reaction queues.
export function flushMicrotasks(turns = 50) {
  let p = Promise.resolve();
  for (let i = 0; i < turns; i += 1) {
    p = p.then(() => {});
  }
  return p;
}
