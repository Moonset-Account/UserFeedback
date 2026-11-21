/**
 * Open-loop load generator.
 *
 * Open-loop semantics:
 *   1. Every arrival is generated exactly at its scheduled timestamp, even
 *      when every concurrency slot is occupied. Back pressure never shifts
 *      the arrival curve — arrivals that cannot fit anywhere are recorded as
 *      drops at their scheduled instant and are never retried or delayed.
 *   2. At a shared timestamp, completion (slot release) events are processed
 *      before arrivals: the released slot first picks up the earliest item
 *      in the bounded FIFO wait queue, then new arrivals are admitted.
 *   3. Every completed item carries its originally scheduled time, the actual
 *      dispatch time and the completion time, plus queue/service durations,
 *      so callers can independently verify queue time, service time and the
 *      drop count.
 *
 * Completion record shape:
 *   { id, scheduledAt, arrivedAt, dispatchedAt, completedAt, queueMs, serviceMs, payload }
 *
 * Drop record shape:
 *   { id, scheduledAt, arrivedAt, reason: 'queue-full', inFlight, queued, queueCapacity }
 */
export class LoadGenerator {
  #clock;
  #backend;
  #concurrency;
  #queueCapacity;
  #arrivals;
  #inFlight = 0;
  #queue = []; // FIFO of admitted-but-undispatched requests
  #completed = [];
  #dropped = [];

  /**
   * @param {object} opts
   * @param {import('./clock.js').VirtualClock|import('./clock.js').RealClock} opts.clock
   * @param {{ handle: (request: object) => number }} opts.backend
   *        Any object exposing handle(request); typically MockBackend.
   * @param {number} opts.concurrency Number of concurrency slots (>= 1).
   * @param {number} opts.queueCapacity Bounded wait-queue length (>= 0).
   * @param {Array<{time: number, id?: string|number, payload?: unknown}>} opts.arrivals
   *        Scheduled arrivals. Times must be finite, non-negative and
   *        non-decreasing; ids (when present) must be unique.
   */
  constructor({ clock, backend, concurrency, queueCapacity, arrivals }) {
    if (!clock || typeof clock.scheduleAt !== 'function') {
      throw new TypeError('clock must implement scheduleAt(time, fn, priority)');
    }
    if (!backend || typeof backend.handle !== 'function') {
      throw new TypeError('backend must implement handle(request)');
    }
    if (!Number.isInteger(concurrency) || concurrency < 1) {
      throw new RangeError('concurrency must be an integer >= 1');
    }
    if (!Number.isInteger(queueCapacity) || queueCapacity < 0) {
      throw new RangeError('queueCapacity must be an integer >= 0');
    }
    if (!Array.isArray(arrivals)) {
      throw new TypeError('arrivals must be an array');
    }

    let lastTime = -Infinity;
    const seenIds = new Set();
    this.#arrivals = arrivals.map((a, index) => {
      if (!a || typeof a !== 'object') {
        throw new TypeError(`arrivals[${index}] must be an object`);
      }
      const { time, id, payload } = a;
      if (!Number.isFinite(time) || time < 0) {
        throw new RangeError(`arrivals[${index}].time must be a finite non-negative number`);
      }
      if (time < lastTime) {
        throw new RangeError(`arrivals[${index}].time (${time}) is earlier than a previous arrival`);
      }
      lastTime = time;
      const resolvedId = id ?? index;
      if (seenIds.has(resolvedId)) {
        throw new RangeError(`duplicate arrival id: ${resolvedId}`);
      }
      seenIds.add(resolvedId);
      return { time, id: resolvedId, payload };
    });

    this.#clock = clock;
    this.#backend = backend;
    this.#concurrency = concurrency;
    this.#queueCapacity = queueCapacity;
  }

  get inFlight() {
    return this.#inFlight;
  }

  get queuedCount() {
    return this.#queue.length;
  }

  get completed() {
    return this.#completed;
  }

  get dropped() {
    return this.#dropped;
  }

  /** Schedule all arrival events. Caller then pumps the clock (virtual) or awaits idle (real). */
  start() {
    for (const arrival of this.#arrivals) {
      // Priority 1: same-instant completions (priority 0) run first.
      this.#clock.scheduleAt(
        arrival.time,
        () => this.#onArrival(arrival),
        1,
      );
    }
    return this;
  }

  #onArrival(arrival) {
    // arrivedAt is the actual event instant; with strict open-loop scheduling
    // it always equals the planned scheduledAt, but both are recorded.
    const request = {
      id: arrival.id,
      scheduledAt: arrival.time,
      arrivedAt: this.#clock.now(),
      payload: arrival.payload,
    };

    if (this.#inFlight < this.#concurrency) {
      this.#dispatch(request);
      return;
    }
    if (this.#queue.length < this.#queueCapacity) {
      this.#queue.push(request);
      return;
    }
    // No slot and no queue room: drop at the planned instant. The arrival
    // curve itself is untouched — nothing is postponed or retried.
    this.#dropped.push({
      id: request.id,
      scheduledAt: request.scheduledAt,
      arrivedAt: request.arrivedAt,
      reason: 'queue-full',
      inFlight: this.#inFlight,
      queued: this.#queue.length,
      queueCapacity: this.#queueCapacity,
    });
  }

  #dispatch(request) {
    this.#inFlight += 1;
    this.#backend.handle(request);
  }

  /** Invoked by the backend at the controlled completion instant. */
  #onComplete(record) {
    this.#inFlight -= 1;
    // Queue time is derivable from the recorded instants; compute it here so
    // callers can verify queue and service durations independently.
    this.#completed.push({
      ...record,
      queueMs: record.dispatchedAt - record.scheduledAt,
    });

    // Release ordering: first pick up the earliest waiting item, filling the
    // just-freed slot before any same-instant new arrival is considered.
    if (this.#queue.length > 0) {
      const next = this.#queue.shift();
      this.#dispatch(next);
    }
  }

  /**
   * Adapter the mock backend calls. Bound once so it can be passed directly
   * as the onComplete callback.
   */
  get completionHandler() {
    return (record) => this.#onComplete(record);
  }
}
