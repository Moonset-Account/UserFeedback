import { CODES, EvalError } from './errors.js';

/**
 * Encode one argument value into a JSON-safe typed pair.
 *
 * The type tag matters: the string "1", the number 1, the bigint 1n and the
 * boolean true must never share a key. NULL is handled by subqueryKey() as its
 * own state so it is also distinct from a missing argument.
 */
export function encodeArgValue(value) {
  if (value === null) return ['null', null];
  switch (typeof value) {
    case 'string':
      return ['string', value];
    case 'number': {
      if (Number.isNaN(value)) return ['number', 'NaN'];
      if (Object.is(value, -0)) return ['number', '-0'];
      if (value === Infinity) return ['number', 'Infinity'];
      if (value === -Infinity) return ['number', '-Infinity'];
      return ['number', String(value)];
    }
    case 'bigint':
      return ['bigint', value.toString()];
    case 'boolean':
      return ['boolean', value];
    case 'undefined':
      throw new EvalError({
        code: CODES.BAD_KEY,
        reason: 'subquery argument value must not be undefined',
      });
    default:
      if (value instanceof Date) {
        if (Number.isNaN(value.getTime())) {
          throw new EvalError({
            code: CODES.BAD_KEY,
            reason: 'subquery argument Date is invalid',
          });
        }
        return ['date', value.toISOString()];
      }
      throw new EvalError({
        code: CODES.BAD_KEY,
        reason: `unsupported subquery argument value type: ${typeof value}`,
      });
  }
}

/**
 * Build the merge key for one scalar-subquery request.
 *
 * Key components:
 *   [sql, [[argName, state, typedValue], ...]] with args sorted by name.
 *
 * State is one of:
 *   - 'present' : the argument is bound, including NULL
 *   - 'missing' : the correlated field did not exist for this outer row
 *
 * `present NULL` and `missing` deliberately produce different keys. So does an
 * empty argument list (`args: []`), which is simply a request with no argument
 * pairs and cannot collide with either.
 *
 * @param {string} sql
 * @param {Array<{name: string, value?: unknown, missing?: true}>} args
 * @returns {string}
 */
export function subqueryKey(sql, args) {
  if (typeof sql !== 'string' || sql.length === 0) {
    throw new EvalError({
      code: CODES.BAD_KEY,
      reason: 'subquery sql must be a non-empty string',
    });
  }
  if (!Array.isArray(args)) {
    throw new EvalError({
      code: CODES.BAD_KEY,
      reason: 'subquery args must be an array',
    });
  }

  const seen = new Set();
  const pairs = [];
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (
      arg === null ||
      typeof arg !== 'object' ||
      Array.isArray(arg) ||
      typeof arg.name !== 'string' ||
      arg.name.length === 0
    ) {
      throw new EvalError({
        code: CODES.BAD_KEY,
        reason: 'each subquery argument must be { name: string, value? | missing? }',
        path: ['args', String(i)],
      });
    }
    if (seen.has(arg.name)) {
      throw new EvalError({
        code: CODES.DUPLICATE_KEY,
        reason: `duplicate subquery argument name: ${arg.name}`,
        path: ['args'],
        detail: { name: arg.name },
      });
    }
    seen.add(arg.name);

    const hasValue = Object.prototype.hasOwnProperty.call(arg, 'value');
    if (arg.missing === true) {
      if (hasValue) {
        throw new EvalError({
          code: CODES.BAD_KEY,
          reason: `subquery argument ${arg.name} is marked missing but also carries a value`,
          path: ['args', String(i)],
          detail: { name: arg.name },
        });
      }
      pairs.push([arg.name, 'missing', null]);
    } else {
      if (!hasValue) {
        throw new EvalError({
          code: CODES.BAD_KEY,
          reason: `subquery argument ${arg.name} has neither a value nor missing:true`,
          path: ['args', String(i)],
          detail: { name: arg.name },
        });
      }
      pairs.push([arg.name, 'present', encodeArgValue(arg.value)]);
    }
  }

  pairs.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  return JSON.stringify([sql, pairs]);
}

/** Copy and canonicalize (name-sorted) arguments for a runner request. */
export function canonicalArgs(args) {
  const copy = args.map((arg) =>
    arg.missing === true
      ? Object.freeze({ name: arg.name, missing: true })
      : Object.freeze({ name: arg.name, value: arg.value }),
  );
  copy.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  return Object.freeze(copy);
}
