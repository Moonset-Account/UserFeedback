/**
 * Generation-aware service connection pool.
 *
 * Design summary
 * --------------
 * - A discovery event describes an instance {name, generation, address, maxConnections}.
 *   Discovering a new generation of an existing name retires the old one.
 * - Every connection (including a connect that has started but not yet
 *   succeeded) occupies one slot of its generation's maxConnections budget.
 * - Retired generations never lend to new waiters. Connections already lent
 *   out keep working until returned; everything idle is closed immediately
 *   and in-flight connects are closed as soon as they resolve.
 * - Borrow requests enter a FIFO queue. #pump walks the queue in order,
 *   dropping expired requests and assigning connections to the oldest
 *   still-valid request that can be served. A head request that cannot be
 *   served blocks every later request (strict queue order).
 * - A borrow settles exactly once: deadline / abort / success / failure
 *   all race through a single `settled` flag. Double return of a lease is
 *   a no-op and can never wake a second waiter.
 */

import {
  BorrowAbortedError,
  BorrowTimeoutError,
  PoolClosedError,
} from './errors.mjs';

const kGen = Symbol('pool.generation');
const kReturned = Symbol('pool.returned');

export class ConnectionPool {
  #clock;
  #connect;
  #closed = false;

  /** key (name#generation) -> generation record */
  #generations = new Map();
  /** key -> immutable stat snapshot for retired generations */
  #retired = new Map();
  /** key -> live record for retired generations (still holds borrowed conns) */
  #retiredGenRecords = new Map();
  /** FIFO of waiter records */
  #waiters = [];
  #nextSeq = 1;
  #nextConnId = 1;
  /** append-only log of every real close() action performed by the pool */
  #closeLog = [];
  #pumping = false;
  #pumpScheduled = false;
  #lastConnectError = null;

  /**
   * @param {object} options
   * @param {{now():number, setTimeout:Function, clearTimeout:Function}} options.clock
   * @param {Function} options.connect  async (address, discovery) => connection
   *        The returned connection object should expose close(); any object
   *        (duck-typed) is accepted and close() is called when applicable.
   */
  constructor({ clock, connect }) {
    if (!clock || typeof clock.now !== 'function' ||
      typeof clock.setTimeout !== 'function' ||
      typeof clock.clearTimeout !== 'function') {
      throw new TypeError('clock must implement now(), setTimeout() and clearTimeout()');
    }
    if (typeof connect !== 'function') {
      throw new TypeError('connect must be an async function');
    }
    this.#clock = clock;
    this.#connect = connect;
  }

  // ---------------------------------------------------------------- discovery

  /**
   * Feed one discovery event into the pool.
   * Re-discovery of an identical (name, generation) is idempotent; an event
   * older than the newest known generation for the name is ignored.
   */
  discover(event) {
    if (this.#closed) throw new PoolClosedError('pool is closed');
    const { name, generation, address, maxConnections } = this.#validateEvent(event);
    const key = `${name}#${generation}`;

    if (this.#generations.has(key) || this.#retiredGenRecords.has(key)) {
      // Same generation re-announced (including a retired one): heartbeat.
      return { key, retired: null, ignored: false, duplicate: true };
    }

    // Reject stale generations for the same instance name.
    const currentKey = this.#currentByInstance(name);
    if (currentKey) {
      const currentGen = this.#generations.get(currentKey);
      if (currentGen && currentGen.generation > generation) {
        return { key, retired: null, ignored: true, duplicate: false };
      }
    }

    const record = {
      key,
      name,
      generation,
      address,
      maxConnections,
      retired: false,
      retiredAt: null,
      retireReason: null,
      idle: [],                 // usable pooled connections
      borrowed: new Set(),      // connection records currently lent out
      pending: new Set(),       // in-flight connect records
      totalConnects: 0,
      totalConnectFailures: 0,
      totalClosed: 0,
      totalBorrowed: 0,         // cumulative successful lends
    };
    this.#generations.set(key, record);

    let retiredKey = null;
    if (currentKey && currentKey !== key) {
      retiredKey = this.#retire(currentKey, 'generation-superseded');
    }

    this.#schedulePump();
    return { key, retired: retiredKey, ignored: false, duplicate: false };
  }

  #validateEvent(event) {
    if (!event || typeof event !== 'object') {
      throw new TypeError('discovery event must be an object');
    }
    const { name, generation, address, maxConnections } = event;
    if (typeof name !== 'string' || name.length === 0) {
      throw new TypeError('event.name must be a non-empty string');
    }
    if (!Number.isInteger(generation) || generation < 0) {
      throw new TypeError('event.generation must be a non-negative integer');
    }
    if (typeof address !== 'string' || address.length === 0) {
      throw new TypeError('event.address must be a non-empty string');
    }
    if (!Number.isInteger(maxConnections) || maxConnections <= 0) {
      throw new TypeError('event.maxConnections must be a positive integer');
    }
    return { name, generation, address, maxConnections };
  }

  #currentByInstance(name) {
    let best = null;
    for (const gen of this.#generations.values()) {
      if (gen.name === name && !gen.retired &&
        (best === null || gen.generation > this.#generations.get(best).generation)) {
        best = gen.key;
      }
    }
    return best;
  }

  // ------------------------------------------------------------------ borrow

  /**
   * Borrow a connection.
   * @param {object} [options]
   * @param {number} [options.timeoutMs] relative deadline in ms
   * @param {number} [options.deadline]  absolute epoch-ms deadline
   * @param {AbortSignal} [options.signal] cancellation signal
   * @returns {Promise<object>} resolves to a lease:
   *   { connection, instanceName, generation, address, seq, return(), isReturned }
   */
  borrow(options = {}) {
    if (this.#closed) {
      return Promise.reject(new PoolClosedError('pool is closed'));
    }
    const { timeoutMs, deadline, signal } = options;

    let expiresAt = Infinity;
    if (Number.isInteger(timeoutMs) && timeoutMs >= 0) {
      expiresAt = this.#clock.now() + timeoutMs;
    }
    if (Number.isInteger(deadline) && deadline >= 0) {
      expiresAt = Math.min(expiresAt, deadline);
    }
    if (expiresAt !== Infinity && expiresAt <= this.#clock.now()) {
      return Promise.reject(new BorrowTimeoutError('borrow deadline already expired'));
    }
    if (signal && signal.aborted) {
      return Promise.reject(new BorrowAbortedError('borrow already aborted'));
    }

    const waiter = {
      seq: this.#nextSeq++,
      expiresAt,
      signal: signal ?? null,
      timer: null,
      onAbort: null,
      settled: false,
      promise: null,
      resolve: null,
      reject: null,
      /** connects started on behalf of this waiter (prevents double dialing) */
      pendingConnections: new Set(),
    };

    waiter.promise = new Promise((resolve, reject) => {
      waiter.resolve = resolve;
      waiter.reject = reject;
    });

    if (expiresAt !== Infinity) {
      const delay = Math.max(0, expiresAt - this.#clock.now());
      waiter.timer = this.#clock.setTimeout(() => {
        this.#failWaiter(
          waiter,
          new BorrowTimeoutError(`borrow seq=${waiter.seq} timed out at ${expiresAt}`, { seq: waiter.seq }),
        );
      }, delay);
    }
    if (signal) {
      waiter.onAbort = () => {
        const reason = signal.reason instanceof Error
          ? signal.reason
          : new Error('aborted');
        this.#failWaiter(
          waiter,
          new BorrowAbortedError(`borrow seq=${waiter.seq} aborted`, { cause: reason, seq: waiter.seq }),
        );
      };
      signal.addEventListener('abort', waiter.onAbort, { once: true });
    }

    this.#waiters.push(waiter);
    this.#schedulePump();
    return waiter.promise;
  }

  #failWaiter(waiter, err) {
    if (waiter.settled) return;
    waiter.settled = true;
    this.#clearWaiterCallbacks(waiter);
    const idx = this.#waiters.indexOf(waiter);
    if (idx !== -1) this.#waiters.splice(idx, 1);
    // Slots reserved by this waiter's in-flight dials are released when the
    // dials themselves resolve; nothing to free here.
    waiter.reject(err);
    this.#pump();
  }

  #clearWaiterCallbacks(waiter) {
    if (waiter.timer !== null) {
      this.#clock.clearTimeout(waiter.timer);
      waiter.timer = null;
    }
    if (waiter.signal && waiter.onAbort) {
      waiter.signal.removeEventListener('abort', waiter.onAbort);
      waiter.onAbort = null;
    }
  }

  // -------------------------------------------------------------------- pump

  /**
   * Batch all pumps triggered inside one synchronous entry sequence
   * (e.g. several borrow() calls in a row) into a single microtask. This
   * guarantees the pump sees the whole batch of newly queued waiters and
   * can distribute every free generation slot at once, instead of each
   * borrow racing a re-entrancy guard and only seeing its own waiter.
   */
  #schedulePump() {
    if (this.#closed || this.#pumpScheduled) return;
    this.#pumpScheduled = true;
    queueMicrotask(() => {
      this.#pumpScheduled = false;
      this.#pump();
    });
  }

  /**
   * Drive the queue to a fixed point. Runs synchronously to completion;
   * async connect outcomes re-enter through #onConnect* which pump again.
   */
  #pump() {
    if (this.#pumping || this.#closed) return;
    this.#pumping = true;
    try {
      // Single in-order pass. The array always keeps strict entry order;
      // waiters that already hold an in-flight dial are "being prepared"
      // and neither block later waiters nor consume queue capacity.
      for (const waiter of [...this.#waiters]) {
        if (waiter.settled) continue; // #failWaiter already removed it

        if (waiter.expiresAt <= this.#clock.now()) {
          this.#failWaiter(
            waiter,
            new BorrowTimeoutError(
              `borrow seq=${waiter.seq} expired before allocation`,
              { seq: waiter.seq },
            ),
          );
          continue;
        }
        if (waiter.signal && waiter.signal.aborted) {
          const reason = waiter.signal.reason instanceof Error
            ? waiter.signal.reason
            : new Error('aborted');
          this.#failWaiter(
            waiter,
            new BorrowAbortedError(`borrow seq=${waiter.seq} aborted`, { cause: reason, seq: waiter.seq }),
          );
          continue;
        }

        // Already being prepared by an unresolved dial: keep its slot,
        // let later waiters be considered in this same pass.
        if (waiter.pendingConnections.size > 0) continue;

        // Reuse an idle connection from any active generation.
        if (this.#tryLendIdle(waiter)) continue;

        // Start one new connect. 'dialing' reserves a slot; move on to the
        // next waiter so other free generations are used immediately.
        const dialed = this.#startConnectFor(waiter);
        if (dialed === 'dialing') continue;

        // 'no-capacity': the oldest waiter that cannot be served blocks all
        // later waiters, preserving strict FIFO allocation order.
        break;
      }
    } finally {
      this.#pumping = false;
    }
  }

  #tryLendIdle(waiter) {
    for (const gen of this.#generations.values()) {
      if (gen.retired || gen.idle.length === 0) continue;
      const conn = gen.idle.shift();
      this.#lend(gen, conn, waiter);
      return true;
    }
    return false;
  }

  /**
   * Precondition: waiter has no in-flight dial.
   * @returns {'dialing'|'no-capacity'}
   *  - dialing:     a new connect was started and now reserves a slot
   *  - no-capacity: no active generation has a free slot for this waiter
   */
  #startConnectFor(waiter) {
    for (const gen of this.#generations.values()) {
      if (gen.retired) continue;
      const reserved = gen.borrowed.size + gen.idle.length + gen.pending.size;
      if (reserved >= gen.maxConnections) continue;

      const connectRecord = {
        id: this.#nextConnId++,
        gen,
        waiter,
        settled: false,
      };
      gen.pending.add(connectRecord);
      waiter.pendingConnections.add(connectRecord);

      // Normalise synchronous throws and plain values into a promise.
      Promise.resolve()
        .then(() => this.#connect(gen.address, {
          name: gen.name,
          generation: gen.generation,
          address: gen.address,
          maxConnections: gen.maxConnections,
        }))
        .then(
          (connection) => this.#onConnectSuccess(connectRecord, connection),
          (cause) => this.#onConnectFailure(connectRecord, cause),
        );
      return 'dialing';
    }
    return 'no-capacity';
  }

  #onConnectSuccess(connectRecord, connection) {
    if (connectRecord.settled) return;
    connectRecord.settled = true;
    const { gen, waiter } = connectRecord;
    gen.pending.delete(connectRecord);
    waiter.pendingConnections.delete(connectRecord);

    if (connection === null || connection === undefined || typeof connection !== 'object') {
      gen.totalConnectFailures += 1;
      this.#pump();
      return;
    }

    gen.totalConnects += 1;
    const conn = { id: connectRecord.id, raw: connection, gen, closed: false, lease: null };

    // The whole pool closed while the dial was in flight: nobody can use it.
    if (this.#closed) {
      this.#closeConnection(gen, conn, 'pool-close');
      return;
    }

    // The generation retired while the dial was in flight: never lend to
    // anyone, close immediately.
    if (gen.retired) {
      this.#closeConnection(gen, conn, 'generation-retired-inflight');
      this.#pump();
      return;
    }

    // The originating waiter is gone (timeout / abort / settled). The
    // connection stays usable for the next waiter of the SAME generation;
    // its slot must not be wasted just because the original requester left.
    const stillQueued = !waiter.settled && this.#waiters.includes(waiter);
    if (stillQueued) {
      this.#lend(gen, conn, waiter);
    } else {
      gen.idle.push(conn);
      this.#pump();
    }
  }

  #onConnectFailure(connectRecord, cause) {
    if (connectRecord.settled) return;
    connectRecord.settled = true;
    const { gen, waiter } = connectRecord;
    gen.pending.delete(connectRecord);
    waiter.pendingConnections.delete(connectRecord);
    gen.totalConnectFailures += 1;
    if (cause instanceof Error) this.#lastConnectError = cause;

    // The waiter (if still queued) keeps its place; the pump retries this
    // generation's capacity or falls over to another active generation.
    this.#pump();
  }

  // -------------------------------------------------------------------- lend

  #lend(gen, conn, waiter) {
    if (waiter.settled) {
      // Lost a race with timeout/abort: pool the connection instead.
      if (!gen.retired) {
        gen.idle.push(conn);
      } else {
        this.#closeConnection(gen, conn, 'generation-retired-inflight');
      }
      this.#pump();
      return;
    }
    waiter.settled = true;
    this.#clearWaiterCallbacks(waiter);
    const idx = this.#waiters.indexOf(waiter);
    if (idx !== -1) this.#waiters.splice(idx, 1);

    gen.borrowed.add(conn);
    gen.totalBorrowed += 1;

    const lease = {
      seq: waiter.seq,
      connection: conn.raw,
      connectionId: conn.id,
      instanceName: gen.name,
      generation: gen.generation,
      address: gen.address,
      [kGen]: gen,
      [kReturned]: false,
      get isReturned() {
        return lease[kReturned];
      },
      return: () => this.#returnLease(lease, conn),
    };
    conn.lease = lease;
    waiter.resolve(lease);
    this.#pump();
  }

  #returnLease(lease, conn) {
    // Idempotent: duplicate returns are ignored and never re-enter the pump,
    // so no second waiter can be woken by one physical return.
    if (lease[kReturned]) return false;
    lease[kReturned] = true;

    const gen = lease[kGen];
    gen.borrowed.delete(conn);

    if (this.#closed || gen.retired) {
      this.#closeConnection(
        gen,
        conn,
        this.#closed ? 'returned-closed-pool' : 'returned-retired',
      );
      if (!this.#closed) this.#pump();
      return true;
    }

    gen.idle.push(conn);
    this.#pump();
    return true;
  }

  // ----------------------------------------------------------------- retire

  #retire(key, reason) {
    const gen = this.#generations.get(key);
    if (!gen || gen.retired) return null;
    gen.retired = true;
    gen.retiredAt = this.#clock.now();
    gen.retireReason = reason;

    // Idle connections have no borrower and close immediately.
    for (const conn of gen.idle.splice(0)) {
      this.#closeConnection(gen, conn, 'generation-retired-idle');
    }
    // In-flight dials keep their slots until they resolve; their outcomes
    // are handled in #onConnectSuccess / #onConnectFailure. Waiters that
    // were relying on those dials must be free to retry on the new
    // generation immediately.
    for (const dial of gen.pending) {
      dial.waiter.pendingConnections.delete(dial);
    }

    this.#archive(gen);
    return key;
  }

  #archive(gen) {
    // Retired records stay readable through stats() but leave the active
    // map so allocation loops never see them.
    this.#generations.delete(gen.key);
    this.#retiredGenRecords.set(gen.key, gen);
    this.#retired.set(gen.key, this.#snapshotGeneration(gen));
  }

  // ------------------------------------------------------------------- close

  /** Close the pool: reject all waiters, close every pooled connection. */
  close() {
    if (this.#closed) return;
    this.#closed = true;

    for (const waiter of [...this.#waiters]) {
      if (!waiter.settled) {
        waiter.settled = true;
        this.#clearWaiterCallbacks(waiter);
        waiter.reject(new PoolClosedError(
          `pool closed while borrow seq=${waiter.seq} was queued`,
          { seq: waiter.seq },
        ));
      }
    }
    this.#waiters.length = 0;

    for (const gen of [...this.#generations.values()]) {
      for (const conn of gen.idle.splice(0)) {
        this.#closeConnection(gen, conn, 'pool-close');
      }
      for (const dial of [...gen.pending]) {
        // The slot frees when the dial resolves; outcome handlers close it.
        dial.waiter.pendingConnections.delete(dial);
      }
      this.#archive(gen);
    }
  }

  #closeConnection(gen, conn, reason) {
    if (conn.closed) return false;
    conn.closed = true;
    gen.totalClosed += 1;
    let closeError = null;
    try {
      const raw = conn.raw;
      if (raw && typeof raw.close === 'function') {
        raw.close();
      }
    } catch (err) {
      closeError = err;
    }
    this.#closeLog.push({
      connectionId: conn.id,
      key: gen.key,
      name: gen.name,
      generation: gen.generation,
      address: gen.address,
      reason,
      at: this.#clock.now(),
      closeError: closeError ? String(closeError) : null,
    });
    return true;
  }

  // ------------------------------------------------------------------- stats

  #snapshotGeneration(gen) {
    return {
      key: gen.key,
      name: gen.name,
      generation: gen.generation,
      address: gen.address,
      maxConnections: gen.maxConnections,
      active: !gen.retired,
      retireReason: gen.retireReason ?? null,
      borrowedCount: gen.borrowed.size,
      idleCount: gen.idle.length,
      pendingConnects: gen.pending.size,
      activeConnections: gen.borrowed.size + gen.idle.length,
      availableCapacity: Math.max(
        0,
        gen.maxConnections - gen.borrowed.size - gen.idle.length - gen.pending.size,
      ),
      totalConnects: gen.totalConnects,
      totalConnectFailures: gen.totalConnectFailures,
      totalClosed: gen.totalClosed,
      totalBorrowed: gen.totalBorrowed,
    };
  }

  /** Read-only observability snapshot used by tests and operators. */
  stats() {
    const generations = [];
    for (const gen of this.#generations.values()) {
      generations.push(this.#snapshotGeneration(gen));
    }
    for (const [key, snap] of this.#retired) {
      // Retired generations may still hold borrowed/in-flight connections;
      // refresh the live counters from the retained record.
      const live = this.#retiredGenRecords.get(key);
      generations.push(live ? this.#snapshotGeneration(live) : snap);
    }
    const waitingForSlot = this.#waiters.filter(
      (w) => !w.settled && w.pendingConnections.size === 0,
    ).length;
    const beingPrepared = this.#waiters.filter(
      (w) => !w.settled && w.pendingConnections.size > 0,
    ).length;
    return {
      closed: this.#closed,
      // queueDepth = requests with no connection and no in-flight dial yet.
      queueDepth: waitingForSlot,
      // Requests whose dial is already running (still occupying a slot).
      pendingWaiters: beingPrepared,
      totalWaiters: waitingForSlot + beingPrepared,
      generations,
      closeEvents: [...this.#closeLog],
    };
  }

  get closed() {
    return this.#closed;
  }

  /** Last connect failure (diagnostics only). */
  get lastConnectError() {
    return this.#lastConnectError;
  }
}

export { BorrowTimeoutError, BorrowAbortedError, PoolClosedError };
export { ConnectFailedError } from './errors.mjs';
