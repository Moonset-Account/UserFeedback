// Manually controllable dial function.
//
// Every call returns a deferred handle exposing:
//   seq        - 0-based order in which the dial was started
//   address    - candidate address
//   family     - 'ipv6' | 'ipv4'
//   signal     - the AbortSignal the dialer passed in
//   dialedAt   - clock time at which the dial started
//   resolve()  - fulfill with a fake connection (no-op once settled)
//   reject(e)  - reject with an error (no-op once settled)
//   forceResolve() / forceReject()
//              - settle even after an abort was observed, simulating a dial
//                that ignores cancellation (success/failure arriving late)
//   settled    - whether the attempt has settled
//
// By default the fake dial cooperates with AbortSignal: an abort before
// settlement rejects the attempt automatically, matching typical real dialers.
export class FakeDial {
  #attempts = [];
  #clock;

  constructor(clock = null) {
    this.#clock = clock;
    const self = this;
    this.fn = (address, family, signal) => {
      const attempt = {
        seq: self.#attempts.length,
        address,
        family,
        signal,
        dialedAt: self.#clock ? self.#clock.now() : null,
        settled: false,
      };
      let resolveHandle;
      let rejectHandle;
      const promise = new Promise((resolve, reject) => {
        resolveHandle = resolve;
        rejectHandle = reject;
      });

      const onAbort = () => {
        if (attempt.settled) return;
        attempt.settled = true;
        signal.removeEventListener('abort', onAbort);
        rejectHandle(Object.assign(new Error(`dial aborted: ${address}`), { aborted: true }));
      };
      if (signal.aborted) {
        onAbort();
      } else {
        signal.addEventListener('abort', onAbort, { once: true });
      }

      attempt.connection = makeConnection(address);
      const detach = () => signal.removeEventListener('abort', onAbort);

      attempt.resolve = () => {
        if (attempt.settled) return;
        attempt.settled = true;
        detach();
        resolveHandle(attempt.connection);
      };
      attempt.reject = (error) => {
        if (attempt.settled) return;
        attempt.settled = true;
        detach();
        rejectHandle(error ?? new Error(`dial failed: ${address}`));
      };
      // A dial that does not cooperate with cancellation can still come back
      // after abort(); these helpers model that late settlement.
      attempt.forceResolve = () => {
        attempt.settled = true;
        detach();
        resolveHandle(attempt.connection);
      };
      attempt.forceReject = (error) => {
        attempt.settled = true;
        detach();
        rejectHandle(error ?? new Error(`dial failed late: ${address}`));
      };
      attempt.promise = promise;
      self.#attempts.push(attempt);
      return promise;
    };
  }

  get attempts() {
    return [...this.#attempts];
  }

  get count() {
    return this.#attempts.length;
  }

  at(index) {
    return this.#attempts[index];
  }

  last() {
    return this.#attempts[this.#attempts.length - 1];
  }

  // All currently unresolved attempts, in start order.
  pending() {
    return this.#attempts.filter((a) => !a.settled);
  }
}

export function makeConnection(label) {
  return {
    label,
    closed: false,
    closeCount: 0,
    close() {
      this.closeCount += 1;
      this.closed = true;
      return Promise.resolve();
    },
  };
}

// Connection whose close() rejects: cleanup failures must be swallowed.
export function makeFailingCloseConnection(label) {
  return {
    label,
    closeCount: 0,
    close() {
      this.closeCount += 1;
      return Promise.reject(new Error('close failed'));
    },
  };
}
