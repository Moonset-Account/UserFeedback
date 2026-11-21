import { ExpressionError } from './errors.js';

const COMPARATORS = {
  eq: (a, b) => a === b,
  neq: (a, b) => a !== b,
  lt: (a, b) => a < b,
  lte: (a, b) => a <= b,
  gt: (a, b) => a > b,
  gte: (a, b) => a >= b,
};

const COMPARATOR_SYMBOLS = {
  eq: '=',
  neq: '<>',
  lt: '<',
  lte: '<=',
  gt: '>',
  gte: '>=',
};

const VALID_KINDS = new Set([
  'column',
  'literal',
  'compare',
  'and',
  'or',
  'div',
  'case',
]);

/**
 * Validate the expression tree once at compile time so malformed trees fail
 * fast instead of failing on some later row.
 */
function validate(node, path = '$') {
  if (node === null || typeof node !== 'object' || Array.isArray(node)) {
    throw new Error(`Invalid expression node at ${path}: expected an object`);
  }
  const { kind } = node;
  if (!VALID_KINDS.has(kind)) {
    throw new Error(`Unknown expression kind at ${path}: ${String(kind)}`);
  }
  switch (kind) {
    case 'column':
      if (typeof node.name !== 'string' || node.name.length === 0) {
        throw new Error(`column node at ${path} requires a non-empty string name`);
      }
      break;
    case 'literal':
      if (node.value !== null && !Number.isInteger(node.value) && typeof node.value !== 'boolean') {
        throw new Error(`literal node at ${path} only supports integer, boolean or null values`);
      }
      break;
    case 'compare':
      if (!Object.prototype.hasOwnProperty.call(COMPARATORS, node.op)) {
        throw new Error(`Unsupported comparison op at ${path}: ${String(node.op)}`);
      }
      validate(node.left, `${path}.left`);
      validate(node.right, `${path}.right`);
      break;
    case 'and':
    case 'or':
      validate(node.left, `${path}.left`);
      validate(node.right, `${path}.right`);
      break;
    case 'div':
      validate(node.left, `${path}.left`);
      validate(node.right, `${path}.right`);
      break;
    case 'case':
      if (!Array.isArray(node.branches) || node.branches.length === 0) {
        throw new Error(`case node at ${path} requires a non-empty branches array`);
      }
      node.branches.forEach((branch, i) => {
        validate(branch.when, `${path}.branches[${i}].when`);
        validate(branch.then, `${path}.branches[${i}].then`);
      });
      if (node.else !== undefined && node.else !== null) {
        validate(node.else, `${path}.else`);
      }
      break;
    default:
      break;
  }
}

/**
 * Truncate a finite number toward zero (SQL-style integer division).
 * @param {number} n
 * @returns {number}
 */
function truncateTowardZero(n) {
  // Add 0 to normalize -0 (e.g. ceil(-0.9)) to +0.
  return (n < 0 ? Math.ceil(n) : Math.floor(n)) + 0;
}

function fail(row, code, message, details = undefined) {
  throw new ExpressionError(code, row, message, details);
}

function requireBoolean(value, node, row) {
  if (value === null) return null;
  if (typeof value !== 'boolean') {
    fail(row, 'TYPE_ERROR', 'AND/OR or CASE condition did not evaluate to a boolean', {
      kind: node.kind,
      actual: typeof value,
    });
  }
  return value;
}

/**
 * Create the row-scoped evaluation function for one compiled expression.
 * No values are cached across rows or batches: every column read happens
 * against the current batch's buffers, which guarantees batch isolation.
 */
function createEvaluator(expr) {
  const evaluate = (node, ctx) => {
    const { columns, validity, row } = ctx;

    switch (node.kind) {
      case 'literal':
        return node.value;

      case 'column': {
        const column = columns[node.name];
        if (column === undefined) {
          fail(row, 'UNKNOWN_COLUMN', `Unknown column: ${node.name}`, { column: node.name });
        }
        const bitmap = validity[node.name];
        // A missing/invalid validity bitmap means the value itself decides
        // nullness; otherwise an explicit 0/false marker (bitmap must cover
        // this row) is authoritative (SQL null marker).
        if (bitmap !== undefined && row < bitmap.length && !bitmap[row]) return null;
        if (column[row] === null || column[row] === undefined) return null;
        return column[row];
      }

      case 'compare': {
        const left = evaluate(node.left, ctx);
        const right = evaluate(node.right, ctx);
        // SQL: any NULL operand makes the comparison result NULL.
        if (left === null || right === null) return null;
        if (typeof left !== typeof right) {
          fail(row, 'TYPE_ERROR', `Cannot compare values of different types (${typeof left} vs ${typeof right})`, {
            op: node.op,
          });
        }
        return COMPARATORS[node.op](left, right);
      }

      case 'and': {
        const left = requireBoolean(evaluate(node.left, ctx), node.left, row);
        // Short circuit: false AND x -> false (right side must not run).
        if (left === false) return false;
        const right = requireBoolean(evaluate(node.right, ctx), node.right, row);
        if (left === null) {
          // NULL AND false -> false; NULL AND (true|NULL) -> NULL.
          return right === false ? false : null;
        }
        // left === true
        if (right === null) return null;
        return right;
      }

      case 'or': {
        const left = requireBoolean(evaluate(node.left, ctx), node.left, row);
        // Short circuit: true OR x -> true (right side must not run).
        if (left === true) return true;
        const right = requireBoolean(evaluate(node.right, ctx), node.right, row);
        if (left === null) {
          // NULL OR true -> true; NULL OR (false|NULL) -> NULL.
          return right === true ? true : null;
        }
        // left === false
        if (right === null) return null;
        return right;
      }

      case 'div': {
        const numerator = evaluate(node.left, ctx);
        // NULL numerator: result is NULL and the denominator must not run,
        // otherwise an unvisited zero denominator could raise a phantom error.
        if (numerator === null) return null;
        const denominator = evaluate(node.right, ctx);
        if (denominator === null) return null;
        if (typeof numerator !== 'number' || typeof denominator !== 'number') {
          fail(row, 'TYPE_ERROR', 'Integer division requires integer operands', {
            numeratorType: typeof numerator,
            denominatorType: typeof denominator,
          });
        }
        if (denominator === 0) {
          fail(row, 'DIVISION_BY_ZERO', 'Division by zero', { numerator });
        }
        return truncateTowardZero(numerator / denominator);
      }

      case 'case': {
        for (const branch of node.branches) {
          const condition = requireBoolean(evaluate(branch.when, ctx), branch.when, row);
          // Only an explicitly TRUE condition enters its branch. NULL and
          // false fall through, and un-taken then-branches are never evaluated.
          if (condition === true) {
            return evaluate(branch.then, ctx);
          }
        }
        // Empty (missing) condition -> the ELSE branch; null when absent.
        if (node.else !== undefined && node.else !== null) {
          return evaluate(node.else, ctx);
        }
        return null;
      }

      default:
        // Unreachable: validate() already rejected unknown kinds.
        fail(row, 'INVALID_EXPRESSION', `Unknown expression kind: ${node.kind}`);
    }
  };

  return evaluate;
}

/**
 * Compile an expression tree into a reusable, batch-isolated executor.
 *
 * @param {object} expression AST with kinds:
 *   - { kind: 'column', name }
 *   - { kind: 'literal', value }          // integer, boolean or null
 *   - { kind: 'compare', op, left, right } // op: eq|neq|lt|lte|gt|gte
 *   - { kind: 'and'|'or', left, right }
 *   - { kind: 'div', left, right }        // integer division, trunc toward 0
 *   - { kind: 'case', branches: [{when, then}], else? }
 * @returns {{ execute: Function }}
 */
export function compile(expression) {
  validate(expression);
  const evaluate = createEvaluator(expression);

  /**
   * Run the compiled expression over one batch.
   *
   * @param {object}   options
   * @param {Object<string, Array>} options.columns    Column name -> value array.
   * @param {Object<string, Array<boolean|number>>} [options.validity]
   *        Per-column validity bitmaps (1/true = valid). Omitted columns rely
   *        on null values inside the column itself.
   * @param {Array<number>} options.selectedRows       Original input row numbers,
   *        selection order is preserved in the output.
   * @returns {{ outputs: Array, errors: Array, results: Array }}
   *          - outputs: successful rows in selection order
   *          - errors:  per-row errors in selection order
   *          - results: interleaved {status:'value',row,value} /
   *                     {status:'error',row,error} entries in selection order
   */
  function execute({ columns = {}, validity = {}, selectedRows = [] } = {}) {
    if (!Array.isArray(selectedRows)) {
      throw new Error('selectedRows must be an array of row numbers');
    }
    const columnNames = Object.keys(columns);
    const batchSize = columnNames.reduce(
      (max, name) => Math.max(max, columns[name].length),
      0,
    );

    const outputs = [];
    const errors = [];
    const results = [];

    for (const row of selectedRows) {
      if (!Number.isInteger(row) || row < 0 || row >= batchSize) {
        const error = new ExpressionError(
          'ROW_OUT_OF_RANGE',
          row,
          `Selected row ${String(row)} is outside the batch (size ${batchSize})`,
          { batchSize },
        );
        errors.push(error.toJSON());
        results.push({ status: 'error', row, error: error.toJSON() });
        continue;
      }

      const ctx = { columns, validity, row };
      try {
        const value = evaluate(expression, ctx);
        outputs.push({ row, value });
        results.push({ status: 'value', row, value });
      } catch (err) {
        if (err instanceof ExpressionError) {
          errors.push(err.toJSON());
          results.push({ status: 'error', row, error: err.toJSON() });
        } else {
          throw err;
        }
      }
    }

    return { outputs, errors, results };
  }

  return { execute };
}

export { COMPARATOR_SYMBOLS, truncateTowardZero };
