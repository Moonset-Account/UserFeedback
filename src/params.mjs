// Canonical, protocol-level parameter handling.
//
// Callers hand us transport-parsed values. We accept JSON-compatible data only
// and derive a stable identity for it. Object member order never changes the
// identity; array order and primitive value distinctions always do.

export class ParamsError extends TypeError {
  constructor(message) {
    super(message);
    this.name = 'ParamsError';
    this.code = 'INVALID_PARAMS';
  }
}

/** Reject anything that has no unambiguous, order-independent identity. */
export function validateParams(params) {
  const seen = new Set();
  const walk = (value, path) => {
    if (value === null) return;
    switch (typeof value) {
      case 'string':
      case 'boolean':
        return;
      case 'number':
        if (!Number.isFinite(value)) {
          throw new ParamsError(`${path}: non-finite numbers are not accepted`);
        }
        return;
      case 'object': {
        if (seen.has(value)) {
          throw new ParamsError(`${path}: cyclic structures are not accepted`);
        }
        if (!Array.isArray(value)) {
          const proto = Object.getPrototypeOf(value);
          if (proto !== Object.prototype && proto !== null) {
            // A Date, Map, RegExp or class instance has its identity in hidden
            // state that this serialisation cannot see; refuse it rather than
            // silently collide with a plain object of the same own keys.
            const name = value?.constructor?.name ?? 'unknown';
            throw new ParamsError(`${path}: ${name} is not a plain JSON-compatible object`);
          }
          if (Object.getOwnPropertySymbols(value).length > 0) {
            throw new ParamsError(`${path}: symbol-keyed properties are not accepted`);
          }
        }
        seen.add(value);
        if (Array.isArray(value)) {
          for (const item of value) walk(item, `${path}[]`);
        } else {
          for (const key of Object.keys(value)) walk(value[key], `${path}.${key}`);
        }
        seen.delete(value);
        return;
      }
      default:
        throw new ParamsError(`${path}: ${typeof value} is not a JSON-compatible value`);
    }
  };
  walk(params, 'params');
  return params;
}

function tag(number) {
  // Object.is distinguishes 0 from -0; Number::toString does not.
  return Object.is(number, -0) ? 'n:-0' : `n:${number}`;
}

function serialise(value) {
  if (value === null) return 'null';
  switch (typeof value) {
    case 'string':
      return `s:${JSON.stringify(value)}`;
    case 'boolean':
      return `b:${value}`;
    case 'number':
      return tag(value);
    case 'object':
      if (Array.isArray(value)) return `a:[${value.map(serialise).join(',')}]`;
      return `o:{${Object.keys(value)
        .sort()
        .map((key) => `${JSON.stringify(key)}=${serialise(value[key])}`)
        .join(',')}}`;
    default:
      throw new ParamsError(`unsupported value of type ${typeof value}`);
  }
}

/** Identity of a parameter value: equal iff the business sees the same effect. */
export function fingerprint(params) {
  return serialise(params);
}

/** Effective retry deadline; `at + ttl` is exact while within the safe range. */
export function retryDeadline(at, ttl) {
  if (!Number.isSafeInteger(at) || at < 0) {
    throw new RangeError('at must be a non-negative safe integer');
  }
  if (!Number.isSafeInteger(ttl) || ttl <= 0) {
    throw new RangeError('retryTtl must be a positive safe integer');
  }
  const deadline = at + ttl;
  if (!Number.isSafeInteger(deadline)) {
    throw new RangeError('retryTtl overflows the safe integer range');
  }
  return deadline;
}
