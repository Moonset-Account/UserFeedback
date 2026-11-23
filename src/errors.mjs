/**
 * Typed errors raised by the connection pool.
 * All errors carry the waiter sequence number where applicable so callers
 * can correlate a rejection with a particular borrow attempt.
 */

export class PoolError extends Error {
  constructor(message, options = {}) {
    super(message);
    this.name = 'PoolError';
    if (options.cause !== undefined) this.cause = options.cause;
    if (options.seq !== undefined) this.seq = options.seq;
  }
}

/** A queued borrow reached its deadline before a connection was granted. */
export class BorrowTimeoutError extends PoolError {
  constructor(message, options = {}) {
    super(message, options);
    this.name = 'BorrowTimeoutError';
    this.code = 'BORROW_TIMEOUT';
  }
}

/** A queued borrow was cancelled through its AbortSignal. */
export class BorrowAbortedError extends PoolError {
  constructor(message, options = {}) {
    super(message, options);
    this.name = 'BorrowAbortedError';
    this.code = 'BORROW_ABORTED';
  }
}

/** The pool (or the target instance generation) is shutting down. */
export class PoolClosedError extends PoolError {
  constructor(message, options = {}) {
    super(message, options);
    this.name = 'PoolClosedError';
    this.code = 'POOL_CLOSED';
  }
}

/** The user supplied connect callback rejected or threw while connecting. */
export class ConnectFailedError extends PoolError {
  constructor(message, options = {}) {
    super(message, options);
    this.name = 'ConnectFailedError';
    this.code = 'CONNECT_FAILED';
  }
}
