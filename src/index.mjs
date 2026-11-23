/**
 * Public entry point for the generation-aware connection pool.
 */
export { ConnectionPool } from './pool.mjs';
export {
  PoolError,
  BorrowTimeoutError,
  BorrowAbortedError,
  PoolClosedError,
  ConnectFailedError,
} from './errors.mjs';
export { ManualClock, systemClock } from './clock.mjs';
