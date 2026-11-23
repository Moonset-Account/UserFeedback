/**
 * Error hierarchy used by the edge cache.
 *
 * - CacheError: base class, carries a machine readable `code`.
 * - UpstreamError: the origin failed (network error, 5xx, or a 304 that
 *   cannot be applied to any cached entity).
 * - TimeoutError: a waiter's individual deadline elapsed before the
 *   coalesced upstream operation settled.
 */
export class CacheError extends Error {
  constructor(message, { cause, code } = {}) {
    super(cause ? `${message}: ${cause.message ?? cause}` : message);
    this.name = 'CacheError';
    this.code = code ?? 'CACHE_ERROR';
    if (cause) this.cause = cause;
  }
}

export class UpstreamError extends CacheError {
  constructor(message, options = {}) {
    super(message, { ...options, code: options.code ?? 'UPSTREAM_FAILURE' });
    this.name = 'UpstreamError';
  }
}

export class TimeoutError extends CacheError {
  constructor(message, options = {}) {
    super(message, { ...options, code: 'DEADLINE_EXCEEDED' });
    this.name = 'TimeoutError';
  }
}
