/**
 * Stable, machine-readable error type used by every public entry point.
 */
export class AdmissionError extends Error {
  /**
   * @param {string} code
   * @param {string} message
   * @param {Record<string, unknown>} [details]
   */
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'AdmissionError';
    this.code = code;
    this.details = details;
  }
}

export function fail(code, message, details) {
  throw new AdmissionError(code, message, details);
}
