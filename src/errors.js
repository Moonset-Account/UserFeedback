/**
 * A typed, machine-readable coordinator error.
 *
 * Every expected failure (bad fold set, superseded attempt, incomplete round,
 * checkpoint step mismatch, ...) is reported with a stable `code` so callers
 * and tests never need to parse English prose.
 */
export class CoordinatorError extends Error {
  /**
   * @param {string} code stable machine-readable code
   * @param {string} message human readable message
   * @param {Record<string, unknown>} [details] structured context
   */
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'CoordinatorError';
    this.code = code;
    this.details = details;
  }
}
