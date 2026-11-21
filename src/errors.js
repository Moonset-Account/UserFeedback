/**
 * Per-row expression evaluation error.
 *
 * A thrown ExpressionError always carries the original input row number so
 * that the batch executor can report it as a row-level error instead of
 * aborting the whole batch.
 */
export class ExpressionError extends Error {
  /**
   * @param {string} code    Machine readable error code.
   * @param {number} row     Original input row number.
   * @param {string} message Human readable message.
   * @param {object} [details]
   */
  constructor(code, row, message, details = {}) {
    super(message);
    this.name = 'ExpressionError';
    this.code = code;
    this.row = row;
    this.details = details;
  }

  toJSON() {
    return {
      row: this.row,
      code: this.code,
      message: this.message,
      details: this.details,
    };
  }
}
