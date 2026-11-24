/**
 * Canonical JSON helpers.
 *
 * All values handled by the interpreter are finite JSON values. Objects are
 * compared by a canonical serialisation (object keys sorted recursively), so
 * structurally equal states always produce the same fingerprint.
 */

/**
 * Canonical JSON string for a JSON value. Returns undefined for values that
 * are not representable as canonical JSON (undefined, functions, bigints,
 * symbols, non-finite numbers).
 */
export function canonicalJson(value) {
  if (value === null) return 'null';
  const type = typeof value;
  if (type === 'string' || type === 'boolean') return JSON.stringify(value);
  if (type === 'number') {
    return Number.isFinite(value) ? JSON.stringify(value) : undefined;
  }
  if (Array.isArray(value)) {
    const parts = [];
    for (const item of value) {
      const part = canonicalJson(item);
      if (part === undefined) return undefined;
      parts.push(part);
    }
    return `[${parts.join(',')}]`;
  }
  if (type === 'object') {
    const proto = Object.getPrototypeOf(value);
    if (proto !== Object.prototype && proto !== null) return undefined;
    const keys = Object.keys(value).sort();
    const parts = [];
    for (const key of keys) {
      const part = canonicalJson(value[key]);
      if (part === undefined) return undefined;
      parts.push(`${JSON.stringify(key)}:${part}`);
    }
    return `{${parts.join(',')}}`;
  }
  return undefined;
}

/** True when the value is a finite, acyclic JSON value. */
export function isJsonValue(value, seen = new Set()) {
  if (value === null) return true;
  const type = typeof value;
  if (type === 'string' || type === 'boolean') return true;
  if (type === 'number') return Number.isFinite(value);
  if (type !== 'object') return false;
  if (seen.has(value)) return false;
  seen.add(value);
  let ok;
  if (Array.isArray(value)) {
    ok = value.every((item) => isJsonValue(item, seen));
  } else {
    const proto = Object.getPrototypeOf(value);
    if (proto !== Object.prototype && proto !== null) {
      seen.delete(value);
      return false;
    }
    ok = Object.values(value).every((item) => isJsonValue(item, seen));
  }
  seen.delete(value);
  return ok;
}

export function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return false;
  }
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/** Structural equality for canonical JSON values. */
export function deepEqual(a, b) {
  const left = canonicalJson(a);
  const right = canonicalJson(b);
  return left !== undefined && left === right;
}

/** Deep clone for JSON values. */
export function cloneJson(value) {
  return structuredClone(value);
}

/** Canonical fingerprint of an object state. */
export function objectFingerprint(object) {
  const keys = Object.keys(object).sort();
  const parts = keys.map(
    (key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`,
  );
  return `{${parts.join(',')}}`;
}
