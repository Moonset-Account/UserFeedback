import { AdmissionError } from './errors.mjs';
import { canonicalJson } from './canonical.mjs';

/**
 * Condition language.
 *
 *   { kind: 'eq',      field, value }        field present and equal to value
 *   { kind: 'missing', field }               field absent
 *   { kind: 'in',      field, values: [] }   field present and value in set
 *   { kind: 'all',     of: [] }              every sub-condition holds
 *   { kind: 'any',     of: [] }              at least one sub-condition holds
 *   { kind: 'not',     of: cond }            sub-condition does not hold
 *
 * `eq` and `in` are strict about presence: a missing field never satisfies
 * them. Use `not({kind:'missing'})` to express "present with any value".
 */

const KINDS = new Set(['eq', 'missing', 'in', 'all', 'any', 'not']);

/**
 * Validate and normalise a raw condition. Returns a frozen canonical copy and
 * the set of fields it reads.
 */
export function compileCondition(raw, path = 'condition') {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new AdmissionError(
      'INVALID_CONDITION',
      `${path}: condition must be an object`,
      { path },
    );
  }
  const kind = raw.kind;
  if (!KINDS.has(kind)) {
    throw new AdmissionError(
      'INVALID_CONDITION',
      `${path}: unknown condition kind ${JSON.stringify(kind)}`,
      { path, kind },
    );
  }
  switch (kind) {
    case 'eq': {
      const field = requireField(raw.field, path);
      if (!('value' in raw)) {
        throw new AdmissionError(
          'INVALID_CONDITION',
          `${path}: eq condition requires a value`,
          { path },
        );
      }
      if (canonicalJson(raw.value) === undefined) {
        throw new AdmissionError(
          'INVALID_CONDITION',
          `${path}: eq value is not a JSON value`,
          { path },
        );
      }
      return {
        condition: { kind, field, value: structuredClone(raw.value) },
        reads: [field],
      };
    }
    case 'missing': {
      const field = requireField(raw.field, path);
      return { condition: { kind, field }, reads: [field] };
    }
    case 'in': {
      const field = requireField(raw.field, path);
      if (!Array.isArray(raw.values)) {
        throw new AdmissionError(
          'INVALID_CONDITION',
          `${path}: in condition requires a values array`,
          { path },
        );
      }
      const values = raw.values.map((value, index) => {
        if (canonicalJson(value) === undefined) {
          throw new AdmissionError(
            'INVALID_CONDITION',
            `${path}.values[${index}]: not a JSON value`,
            { path, index },
          );
        }
        return structuredClone(value);
      });
      return { condition: { kind, field, values }, reads: [field] };
    }
    case 'all':
    case 'any': {
      if (!Array.isArray(raw.of)) {
        throw new AdmissionError(
          'INVALID_CONDITION',
          `${path}: ${kind} condition requires an of array`,
          { path },
        );
      }
      const reads = new Set();
      const of = raw.of.map((child, index) => {
        const compiled = compileCondition(child, `${path}.of[${index}]`);
        for (const field of compiled.reads) reads.add(field);
        return compiled.condition;
      });
      return { condition: { kind, of }, reads: [...reads] };
    }
    case 'not': {
      const compiled = compileCondition(raw.of, `${path}.of`);
      return { condition: { kind, of: compiled.condition }, reads: compiled.reads };
    }
    default:
      throw new AdmissionError(
        'INVALID_CONDITION',
        `${path}: unsupported condition kind ${kind}`,
        { path, kind },
      );
  }
}

function requireField(field, path) {
  if (typeof field !== 'string' || field.length === 0) {
    throw new AdmissionError(
      'INVALID_CONDITION',
      `${path}: field must be a non-empty string`,
      { path },
    );
  }
  return field;
}

/** Evaluate a compiled condition against an object state. */
export function evaluateCondition(condition, object) {
  switch (condition.kind) {
    case 'eq': {
      if (!Object.hasOwn(object, condition.field)) return false;
      return sameValue(object[condition.field], condition.value);
    }
    case 'missing':
      return !Object.hasOwn(object, condition.field);
    case 'in': {
      if (!Object.hasOwn(object, condition.field)) return false;
      const value = object[condition.field];
      return condition.values.some((candidate) => sameValue(value, candidate));
    }
    case 'all':
      return condition.of.every((child) => evaluateCondition(child, object));
    case 'any':
      return condition.of.some((child) => evaluateCondition(child, object));
    case 'not':
      return !evaluateCondition(condition.of, object);
    default:
      throw new AdmissionError(
        'INVALID_CONDITION',
        `cannot evaluate unknown condition kind ${condition.kind}`,
      );
  }
}

function sameValue(a, b) {
  return canonicalJson(a) === canonicalJson(b);
}

/** Collect all field names referenced by a compiled condition. */
export function conditionFields(condition, out = new Set()) {
  switch (condition.kind) {
    case 'eq':
    case 'missing':
    case 'in':
      out.add(condition.field);
      break;
    case 'all':
    case 'any':
      for (const child of condition.of) conditionFields(child, out);
      break;
    case 'not':
      conditionFields(condition.of, out);
      break;
    default:
      break;
  }
  return out;
}
