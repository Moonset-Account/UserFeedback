/**
 * Deterministic clock and fake scheduler, implemented with the Node.js
 * standard library only.
 *
 * The fake scheduler is injected explicitly into the EdgeCache
 * constructor instead of replacing global setTimeout, so the node:test
 * runner itself keeps using the real timers. Time advances only when a
 * test calls `advance` / `tick`.
 */
export function createClock(startTime = 1_000_000) {
  let current = startTime;
  let nextId = 1;
  /** @type {Map<number, {id:number, due:number, callback:Function, args:any[], cancelled:boolean}>} */
  const scheduled = new Map();

  const fakeSetTimeout = (callback, delay = 0, ...args) => {
    const id = nextId++;
    scheduled.set(id, {
      id,
      due: current + Math.max(0, Number(delay) || 0),
      callback,
      args,
      cancelled: false,
    });
    return id;
  };

  const fakeClearTimeout = (id) => {
    const timer = scheduled.get(id);
    if (timer) timer.cancelled = true;
  };

  return {
    now: () => current,
    setTimeout: fakeSetTimeout,
    clearTimeout: fakeClearTimeout,
    /** Advance time without firing timer callbacks. */
    advance(ms) {
      current += ms;
    },
    /**
     * Advance time and run every timer that becomes due, flushing
     * microtasks between expirations so promise chains settle.
     */
    async tick(ms) {
      const target = current + ms;
      for (;;) {
        const due = [...scheduled.values()]
          .filter((timer) => !timer.cancelled && timer.due <= target)
          .sort((a, b) => a.due - b.due || a.id - b.id);
        if (due.length === 0) break;
        const next = due[0];
        current = Math.max(current, next.due);
        scheduled.delete(next.id);
        next.cancelled = true;
        next.callback(...next.args);
        await flushMicrotasks();
      }
      current = target;
    },
    /** Number of timers still pending (test/inspection helper). */
    pendingTimers() {
      let count = 0;
      for (const timer of scheduled.values()) {
        if (!timer.cancelled) count += 1;
      }
      return count;
    },
  };
}

/** Flush pending microtasks (promise reactions) without advancing time. */
export function flushMicrotasks() {
  return new Promise((resolve) => setImmediate(resolve));
}
