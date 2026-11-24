import { AdmissionError } from './errors.mjs';
import { canonicalJson, isJsonValue, isPlainObject } from './canonical.mjs';
import { compileCondition } from './condition.mjs';

/** Hard limit from the task statement. */
export const MAX_HOOKS = 8;

const RESERVED_FIELDS = new Set(['__proto__']);

/**
 * Validate a program specification and return a normalised, immutable-in-
 * practice program value.
 *
 * spec = {
 *   domain: { field: [allowedValue, ...], ... },
 *   hooks: [ { id?, when: condition, action: {kind:'set', field, value}
 *                                     | {kind:'delete', field} }, ... ],
 *   accept: condition
 * }
 */
export function defineProgram(spec) {
  if (!isPlainObject(spec)) {
    throw new AdmissionError(
      'PROGRAM_INVALID',
      'program spec must be a plain object',
    );
  }
  const domain = parseDomain(spec.domain);
  const hooks = parseHooks(spec.hooks, domain);
  if (!Object.hasOwn(spec, 'accept')) {
    throw new AdmissionError(
      'PROGRAM_INVALID',
      'program spec requires an accept condition',
    );
  }
  const acceptCompiled = compileCondition(spec.accept, 'accept');
  for (const field of acceptCompiled.reads) {
    requireDeclaredField(domain, field, 'accept');
  }
  return {
    kind: 'admission-program',
    fields: domain.fields,
    values: domain.values,
    valueKeys: domain.valueKeys,
    hooks: hooks.list,
    hookIds: hooks.ids,
    hooksById: hooks.byId,
    accept: acceptCompiled.condition,
  };
}

function parseDomain(raw) {
  if (!isPlainObject(raw)) {
    throw new AdmissionError(
      'DOMAIN_INVALID',
      'domain must be a plain object mapping fields to value arrays',
    );
  }
  const fields = Object.keys(raw);
  const values = new Map();
  const valueKeys = new Map();
  for (const field of fields) {
    if (field.length === 0 || RESERVED_FIELDS.has(field)) {
      throw new AdmissionError(
        'DOMAIN_INVALID',
        `domain field name ${JSON.stringify(field)} is not allowed`,
        { field },
      );
    }
    const list = raw[field];
    if (!Array.isArray(list)) {
      throw new AdmissionError(
        'DOMAIN_INVALID',
        `domain.${field} must be an array of allowed values`,
        { field },
      );
    }
    const keys = new Set();
    const normalised = [];
    for (let index = 0; index < list.length; index += 1) {
      const value = list[index];
      if (!isJsonValue(value)) {
        throw new AdmissionError(
          'DOMAIN_INVALID',
          `domain.${field}[${index}] is not a finite JSON value`,
          { field, index },
        );
      }
      const key = canonicalJson(value);
      if (keys.has(key)) {
        throw new AdmissionError(
          'DUPLICATE_DOMAIN_VALUE',
          `domain.${field} contains duplicate value ${key}`,
          { field, index },
        );
      }
      keys.add(key);
      normalised.push(structuredClone(value));
    }
    values.set(field, normalised);
    valueKeys.set(field, keys);
  }
  return { fields, values, valueKeys };
}

function parseHooks(raw, domain) {
  if (!Array.isArray(raw)) {
    throw new AdmissionError(
      'HOOK_INVALID',
      'hooks must be an array',
    );
  }
  if (raw.length > MAX_HOOKS) {
    throw new AdmissionError(
      'TOO_MANY_HOOKS',
      `at most ${MAX_HOOKS} hooks are allowed, received ${raw.length}`,
      { count: raw.length, max: MAX_HOOKS },
    );
  }
  const list = [];
  const ids = [];
  const byId = new Map();
  raw.forEach((hook, index) => {
    if (!isPlainObject(hook)) {
      throw new AdmissionError(
        'HOOK_INVALID',
        `hooks[${index}] must be a plain object`,
        { index },
      );
    }
    const id = hook.id === undefined ? `h${index}` : hook.id;
    if (typeof id !== 'string' || id.length === 0) {
      throw new AdmissionError(
        'HOOK_INVALID',
        `hooks[${index}].id must be a non-empty string`,
        { index },
      );
    }
    if (byId.has(id)) {
      throw new AdmissionError(
        'DUPLICATE_HOOK_ID',
        `duplicate hook id ${JSON.stringify(id)}`,
        { id, index },
      );
    }
    if (!Object.hasOwn(hook, 'when')) {
      throw new AdmissionError(
        'HOOK_INVALID',
        `hooks[${index}] (${id}) requires a when condition`,
        { index, id },
      );
    }
    const whenCompiled = compileCondition(hook.when, `hooks[${index}].when`);
    for (const field of whenCompiled.reads) {
      requireDeclaredField(domain, field, `hooks[${index}].when`);
    }
    const action = parseAction(hook.action, index, id, domain);
    const normalised = Object.freeze({
      id,
      index,
      when: whenCompiled.condition,
      action,
    });
    list.push(normalised);
    ids.push(id);
    byId.set(id, normalised);
  });
  return { list, ids, byId };
}

function parseAction(raw, index, id, domain) {
  if (!isPlainObject(raw)) {
    throw new AdmissionError(
      'HOOK_INVALID',
      `hooks[${index}] (${id}) requires an action object`,
      { index, id },
    );
  }
  if (raw.kind === 'set') {
    const field = requireActionField(domain, raw.field, index, id);
    if (!Object.hasOwn(raw, 'value')) {
      throw new AdmissionError(
        'HOOK_INVALID',
        `hooks[${index}] (${id}) set action requires a value`,
        { index, id },
      );
    }
    if (!isJsonValue(raw.value)) {
      throw new AdmissionError(
        'HOOK_INVALID',
        `hooks[${index}] (${id}) set value is not a finite JSON value`,
        { index, id },
      );
    }
    const key = canonicalJson(raw.value);
    if (!domain.valueKeys.get(field).has(key)) {
      throw new AdmissionError(
        'VALUE_OUT_OF_DOMAIN',
        `hooks[${index}] (${id}) sets ${field} to ${key}, which is not declared in the domain`,
        { index, id, field, value: key },
      );
    }
    return Object.freeze({
      kind: 'set',
      field,
      value: structuredClone(raw.value),
    });
  }
  if (raw.kind === 'delete') {
    const field = requireActionField(domain, raw.field, index, id);
    return Object.freeze({ kind: 'delete', field });
  }
  throw new AdmissionError(
    'HOOK_INVALID',
    `hooks[${index}] (${id}) action kind must be "set" or "delete"`,
    { index, id, kind: raw.kind },
  );
}

function requireActionField(domain, field, index, id) {
  if (typeof field !== 'string' || field.length === 0) {
    throw new AdmissionError(
      'HOOK_INVALID',
      `hooks[${index}] (${id}) action requires a field name`,
      { index, id },
    );
  }
  requireDeclaredField(domain, field, `hooks[${index}].action`);
  return field;
}

function requireDeclaredField(domain, field, context) {
  if (!domain.values.has(field)) {
    throw new AdmissionError(
      'UNKNOWN_FIELD',
      `${context} references undeclared field ${JSON.stringify(field)}`,
      { field, context },
    );
  }
}

/**
 * Canonical signature of a validated program. Evidence records this signature
 * so replay can refuse evidence produced for a different program.
 */
export function programSignature(program) {
  const hooks = [...program.hooks]
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
    .map((hook) => ({ id: hook.id, when: hook.when, action: hook.action }));
  return canonicalJson({
    fields: [...program.fields].sort(),
    values: Object.fromEntries([...program.values.entries()]),
    hooks,
    accept: program.accept,
  });
}

/**
 * Validate a caller-supplied initial object against the domain and return an
 * internal null-prototype state. Missing fields are allowed; undeclared fields
 * and out-of-domain values are not.
 */
export function normalizeInitialObject(program, raw) {
  if (!isPlainObject(raw)) {
    throw new AdmissionError(
      'INITIAL_OBJECT_INVALID',
      'initial object must be a plain object',
    );
  }
  for (const [field, value] of Object.entries(raw)) {
    if (!program.values.has(field)) {
      throw new AdmissionError(
        'INITIAL_OBJECT_INVALID',
        `initial object contains undeclared field ${JSON.stringify(field)}`,
        { field },
      );
    }
    if (!isJsonValue(value)) {
      throw new AdmissionError(
        'INITIAL_OBJECT_INVALID',
        `initial object field ${field} is not a finite JSON value`,
        { field },
      );
    }
    const key = canonicalJson(value);
    if (!program.valueKeys.get(field).has(key)) {
      throw new AdmissionError(
        'INITIAL_OBJECT_INVALID',
        `initial object field ${field} has value ${key}, which is not declared in the domain`,
        { field, value: key },
      );
    }
  }
  const state = Object.create(null);
  for (const field of program.fields) {
    if (Object.hasOwn(raw, field)) {
      state[field] = structuredClone(raw[field]);
    }
  }
  return state;
}
