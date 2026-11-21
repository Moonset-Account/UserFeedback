/**
 * Hookable local mock backend.
 *
 * The backend never owns a clock of its own: it receives the current time
 * (via the request payload and the load generator's clock) and drives
 * completion through the clock abstraction, so callers fully control when
 * each completion callback fires. No real network or timer is involved.
 *
 * Service duration policy:
 *   - serviceTime: number -> every request takes that many ms
 *                   function (request, ordinal) -> custom duration per request
 *   - slowFirst: number of the first N *dispatched* requests that are
 *                deliberately slowed (default 0). After those, the backend
 *                "recovers" and uses the normal service time. This models the
 *                requirement "slow down the first two requests, then recover":
 *                the slowdown affects only in-flight service time and never
 *                shifts the arrival curve, so later arrival ids/times stay put.
 */
export class MockBackend {
  #serviceTime;
  #slowFirst;
  #slowDuration;
  #clock;
  #onComplete;
  #dispatchOrdinal = 0;

  /**
   * @param {object} opts
   * @param {number|(request: object, ordinal: number) => number} opts.serviceTime
   *        Normal service duration in ms (or a function).
   * @param {number} [opts.slowFirst=0] How many leading dispatches are slowed.
   * @param {number} [opts.slowDuration] Duration applied to the slowed requests.
   * @param {import('./clock.js').VirtualClock|import('./clock.js').RealClock} opts.clock
   * @param {(record: object) => void} opts.onComplete
   *        Completion callback invoked at the controlled completion instant.
   */
  constructor({ serviceTime, slowFirst = 0, slowDuration, clock, onComplete }) {
    if (typeof serviceTime !== 'number' && typeof serviceTime !== 'function') {
      throw new TypeError('serviceTime must be a number or a function');
    }
    if (typeof serviceTime === 'number' && (!Number.isFinite(serviceTime) || serviceTime < 0)) {
      throw new RangeError('serviceTime must be a finite non-negative number');
    }
    if (!Number.isInteger(slowFirst) || slowFirst < 0) {
      throw new RangeError('slowFirst must be a non-negative integer');
    }
    if (slowFirst > 0) {
      if (!Number.isFinite(slowDuration) || slowDuration < 0) {
        throw new RangeError('slowDuration must be a finite non-negative number when slowFirst > 0');
      }
    }
    if (!clock || typeof clock.setTimeout !== 'function') {
      throw new TypeError('clock must implement setTimeout(fn, delayMs, priority)');
    }
    if (typeof onComplete !== 'function') {
      throw new TypeError('onComplete must be a function');
    }
    this.#serviceTime = serviceTime;
    this.#slowFirst = slowFirst;
    this.#slowDuration = slowDuration;
    this.#clock = clock;
    this.#onComplete = onComplete;
  }

  /** Number of requests handed to the backend so far (0-based ordinal + 1). */
  get dispatchedCount() {
    return this.#dispatchOrdinal;
  }

  /**
   * Handle one dispatched request and schedule its controlled completion.
   * Completion is scheduled with priority 0, ahead of same-instant arrivals
   * (priority 1). Returns the chosen service duration for observability.
   */
  handle(request) {
    const ordinal = this.#dispatchOrdinal++;
    let duration;
    if (ordinal < this.#slowFirst) {
      duration = this.#slowDuration;
    } else if (typeof this.#serviceTime === 'function') {
      duration = this.#serviceTime(request, ordinal);
      if (!Number.isFinite(duration) || duration < 0) {
        throw new RangeError(`serviceTime function returned an invalid duration: ${duration}`);
      }
    } else {
      duration = this.#serviceTime;
    }

    const dispatchedAt = this.#clock.now();
    this.#clock.setTimeout(
      () => {
        this.#onComplete({
          ...request,
          dispatchedAt,
          completedAt: this.#clock.now(),
          serviceMs: this.#clock.now() - dispatchedAt,
        });
      },
      duration,
      0,
    );
    return duration;
  }
}
