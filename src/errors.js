/** Error types and stable error codes for the batch evaluator. */

export const CODES = Object.freeze({
  DIV_BY_ZERO: 'DIV_BY_ZERO',
  TYPE: 'TYPE',
  SCALAR_CARDINALITY: 'SCALAR_CARDINALITY',
  NULL_CONDITION: 'NULL_CONDITION',
  BAD_KEY: 'BAD_KEY',
  DUPLICATE_KEY: 'DUPLICATE_KEY',
  MISSING_PARAM: 'MISSING_PARAM',
  UNKNOWN_NODE: 'UNKNOWN_NODE',
  RUNNER: 'RUNNER',
  BAD_RESPONSE: 'BAD_RESPONSE',
});

export class EvalError extends Error {
  /**
   * @param {object} info
   * @param {string} info.code    stable machine code from CODES
   * @param {string} info.reason  human readable message
   * @param {number} info.rowIndex original outer row number (0-based), -1 if unknown
   * @param {string[]} info.path  evaluation path inside the row tree
   * @param {object} [info.detail]
   */
  constructor({ code, reason, rowIndex = -1, path = [], detail = null }) {
    super(reason);
    this.name = 'EvalError';
    this.code = code;
    this.reason = reason;
    this.rowIndex = rowIndex;
    this.path = path;
    this.detail = detail;
  }

  toJSON() {
    return {
      code: this.code,
      reason: this.reason,
      rowIndex: this.rowIndex,
      path: this.path,
      detail: this.detail,
    };
  }
}

export const isEvalError = (err) => err instanceof EvalError;
