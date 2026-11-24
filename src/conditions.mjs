/**
 * Deterministic browser-variant loading-condition evaluation.
 */

export class ConditionError extends Error {
  constructor(message, details = undefined) {
    super(message);
    this.name = 'ConditionError';
    this.code = 'INVALID_LOAD_CONDITION';
    this.details = details;
  }
}

function lower(value) {
  return typeof value === 'string' ? value.toLowerCase() : value;
}

export function compareVersionNumbers(a, b) {
  const pa = String(a ?? '0')
    .replace(/^v/, '')
    .split(/[.+-]/)
    .map((part) => Number.parseInt(part, 10) || 0);
  const pb = String(b ?? '0')
    .replace(/^v/, '')
    .split(/[.+-]/)
    .map((part) => Number.parseInt(part, 10) || 0);
  const length = Math.max(pa.length, pb.length);
  for (let i = 0; i < length; i += 1) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x > y) return 1;
    if (x < y) return -1;
  }
  return 0;
}

function evalComparator(version, comparator) {
  const text = String(comparator).trim();
  const match = /^(<=|>=|==|=|<|>)?\s*v?([0-9]+(?:\.[0-9]+){0,2}(?:[-+][0-9A-Za-z.-]+)?)$/.exec(text);
  if (!match) throw new ConditionError(`Unsupported version constraint ${text}`);
  const operator = match[1] ?? '=';
  const expected = match[2];
  const cmp = compareVersionNumbers(version, expected);
  switch (operator) {
    case '<':
      return cmp < 0;
    case '<=':
      return cmp <= 0;
    case '>':
      return cmp > 0;
    case '>=':
      return cmp >= 0;
    case '=':
    case '==':
    default:
      return cmp === 0;
  }
}

function evalVersionConstraint(version, constraint) {
  if (version === undefined || version === null || version === '') {
    throw new ConditionError('Version condition requires context.version');
  }
  if (Array.isArray(constraint)) {
    return constraint.every((part) => evalVersionConstraint(version, part));
  }
  if (typeof constraint !== 'string') {
    throw new ConditionError('Version constraint must be a string or array');
  }
  // Allow ">=120 <130" as whitespace-separated AND comparators.
  const parts = constraint.trim().split(/\s+/).filter(Boolean);
  return parts.every((part) => evalComparator(version, part));
}

function listIncludes(actual, expected) {
  if (expected === undefined) return true;
  const expectedList = Array.isArray(expected) ? expected : [expected];
  const actualList = Array.isArray(actual) ? actual : [actual];
  const actualLower = new Set(actualList.map(lower));
  return expectedList.some((item) => actualLower.has(lower(item)));
}

function flagMatches(flags, name, expected = true) {
  const actual = flags && typeof flags === 'object' ? flags[name] : undefined;
  if (expected === true) return actual === true || actual === 'true' || actual === 1;
  if (expected === false) return actual === false || actual === 'false' || actual === 0 || actual === undefined;
  return actual === expected;
}

const KNOWN_KEYS = new Set([
  'browser',
  'browsers',
  'excludeBrowsers',
  'variant',
  'variants',
  'excludeVariants',
  'os',
  'oses',
  'channel',
  'channels',
  'manifestVersion',
  'manifestVersions',
  'version',
  'minVersion',
  'maxVersion',
  'flag',
  'flags',
  'enabledFeature',
  'enabledFeatures',
  'all',
  'any',
  'not',
  'when',
  'context',
]);

/**
 * Evaluate one loading condition against a variant context.
 *
 * Arrays and objects are AND-ed unless `any` is used.
 *
 * @param {unknown} condition
 * @param {Record<string, any>} [context]
 * @returns {boolean}
 */
export function evaluateCondition(condition, context = {}) {
  if (condition === undefined || condition === null || condition === true) return true;
  if (condition === false) return false;
  if (typeof condition === 'string') {
    const target = lower(context.browser ?? context.variant ?? context.id);
    return target === lower(condition);
  }
  if (typeof condition === 'function') return Boolean(condition(context));
  if (Array.isArray(condition)) return condition.every((item) => evaluateCondition(item, context));
  if (typeof condition !== 'object') {
    throw new ConditionError(`Unsupported condition value: ${String(condition)}`);
  }

  for (const key of Object.keys(condition)) {
    if (!KNOWN_KEYS.has(key)) throw new ConditionError(`Unknown loading-condition key: ${key}`);
  }

  if (condition.all !== undefined && !evaluateCondition(condition.all, context)) return false;
  if (condition.any !== undefined) {
    const alternatives = Array.isArray(condition.any) ? condition.any : [condition.any];
    if (!alternatives.some((item) => evaluateCondition(item, context))) return false;
  }
  if (condition.not !== undefined && evaluateCondition(condition.not, context)) return false;
  if (typeof condition.when === 'function' && !condition.when(context)) return false;

  const browser = context.browser ?? context.variant ?? context.id;
  if (condition.browser !== undefined && lower(browser) !== lower(condition.browser)) return false;
  if (condition.browsers !== undefined && !listIncludes(browser, condition.browsers)) return false;
  if (condition.excludeBrowsers !== undefined && listIncludes(browser, condition.excludeBrowsers)) return false;

  const variant = context.variant ?? context.id;
  if (condition.variant !== undefined && lower(variant) !== lower(condition.variant)) return false;
  if (condition.variants !== undefined && !listIncludes(variant, condition.variants)) return false;
  if (condition.excludeVariants !== undefined && listIncludes(variant, condition.excludeVariants)) return false;

  const os = context.os;
  if (condition.os !== undefined && lower(os) !== lower(condition.os)) return false;
  if (condition.oses !== undefined && !listIncludes(os, condition.oses)) return false;

  if (condition.channel !== undefined && lower(context.channel) !== lower(condition.channel)) return false;
  if (condition.channels !== undefined && !listIncludes(context.channel, condition.channels)) return false;

  if (condition.manifestVersion !== undefined) {
    const accepted = Array.isArray(condition.manifestVersion)
      ? condition.manifestVersion
      : [condition.manifestVersion];
    if (!accepted.includes(context.manifestVersion)) return false;
  }
  if (condition.manifestVersions !== undefined && !listIncludes(context.manifestVersion, condition.manifestVersions)) {
    return false;
  }

  if (condition.version !== undefined && !evalVersionConstraint(context.version, condition.version)) return false;
  if (condition.minVersion !== undefined) {
    if (context.version === undefined) throw new ConditionError('minVersion requires context.version');
    if (compareVersionNumbers(context.version, condition.minVersion) < 0) return false;
  }
  if (condition.maxVersion !== undefined) {
    if (context.version === undefined) throw new ConditionError('maxVersion requires context.version');
    if (compareVersionNumbers(context.version, condition.maxVersion) > 0) return false;
  }

  if (condition.flag !== undefined) {
    if (typeof condition.flag === 'string') {
      if (!flagMatches(context.flags, condition.flag)) return false;
    } else if (condition.flag && typeof condition.flag === 'object') {
      for (const [name, expected] of Object.entries(condition.flag)) {
        if (!flagMatches(context.flags, name, expected)) return false;
      }
    }
  }
  if (condition.flags !== undefined) {
    for (const [name, expected] of Object.entries(condition.flags)) {
      if (!flagMatches(context.flags, name, expected)) return false;
    }
  }

  if (condition.enabledFeature !== undefined && !listIncludes(context.enabledFeatures, condition.enabledFeature)) {
    return false;
  }
  if (
    condition.enabledFeatures !== undefined &&
    !condition.enabledFeatures.every((feature) => listIncludes(context.enabledFeatures, feature))
  ) {
    return false;
  }

  if (condition.context !== undefined) {
    for (const [key, expected] of Object.entries(condition.context)) {
      if (context[key] !== expected) return false;
    }
  }

  return true;
}

/**
 * Normalize variant context from common variant fields.
 * @param {object} variant
 */
export function createVariantContext(variant = {}) {
  const contextOverrides = variant.context && typeof variant.context === 'object' ? variant.context : {};
  return {
    ...contextOverrides,
    variant: variant.id ?? variant.name ?? variant.browser ?? contextOverrides.variant,
    id: variant.id ?? variant.name ?? variant.browser ?? contextOverrides.id,
    browser: variant.browser ?? variant.id ?? variant.name ?? contextOverrides.browser,
    version: variant.version ?? variant.browserVersion ?? contextOverrides.version,
    channel: variant.channel ?? contextOverrides.channel,
    os: variant.os ?? contextOverrides.os,
    manifestVersion: variant.manifestVersion ?? contextOverrides.manifestVersion,
    flags: variant.flags ?? contextOverrides.flags ?? {},
    enabledFeatures:
      variant.enabledFeatures ??
      variant.enabled_features ??
      contextOverrides.enabledFeatures ??
      [],
  };
}
