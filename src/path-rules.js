/**
 * Declared comparison rules and pure path-key computation.
 *
 * These rules are deliberately explicit and platform-independent: the
 * validator must behave identically regardless of the host operating system,
 * because the install target is a *virtual* tree, not the real filesystem.
 */

/**
 * @typedef {object} PathRules
 * @property {'/'} separator          Archive entry paths are split/joined with
 *                                    forward slash only. Backslash is a literal
 *                                    filename character at best, never a
 *                                    separator, and is rejected outright so a
 *                                    Windows-style entry can never smuggle in
 *                                    a path component.
 * @property {'NFC'} unicodeNormalization
 *                                    Every path component is normalized to
 *                                    Unicode NFC before comparison, so names
 *                                    that differ only by canonical
 *                                    decomposition (e.g. a precomposed é vs.
 *                                    e + combining accent) collide.
 * @property {'sensitive'|'insensitive'} caseComparison
 *                                    'sensitive' compares code points as-is
 *                                    after NFC. 'insensitive' additionally
 *                                    folds with the locale-independent Unicode
 *                                    simple-uppercase mapping.
 */

/** @type {PathRules} */
export const DEFAULT_PATH_RULES = Object.freeze({
  separator: '/',
  unicodeNormalization: 'NFC',
  caseComparison: 'sensitive',
});

const RESERVED_NAME = '.';
const PARENT_NAME = '..';

/**
 * Normalize one path component for *display/join*: NFC only, case preserved.
 * @param {string} component
 * @returns {string}
 */
export function normalizeComponent(component) {
  return component.normalize('NFC');
}

/**
 * Compute the comparison key for one component under the declared rules.
 * @param {string} component
 * @param {PathRules} [rules]
 * @returns {string}
 */
export function comparisonKey(component, rules = DEFAULT_PATH_RULES) {
  const nfc = normalizeComponent(component);
  return rules.caseComparison === 'insensitive'
    ? nfc.toUpperCase()
    : nfc;
}

/**
 * Compute the comparison key for a whole relative path (already split into
 * components). The key is only used for equality/collision checks; the
 * NFC-normalized original spelling is what appears in the plan.
 * @param {readonly string[]} components
 * @param {PathRules} [rules]
 * @returns {string}
 */
export function comparisonKeyForPath(components, rules = DEFAULT_PATH_RULES) {
  return components.map((c) => comparisonKey(c, rules)).join(rules.separator);
}

/**
 * @typedef {object} PathCheck
 * @property {true} ok
 * @property {string[]} components   NFC-normalized components, original case
 * @property {string} normalizedPath Components rejoined with the separator
 */

/**
 * Structural checks on a raw archive entry path.
 *
 * Rejected here (before any target computation):
 *  - non-string / empty path
 *  - embedded NUL bytes
 *  - backslash (declared separator is '/'; no ambiguity allowed)
 *  - absolute POSIX path ('/...') or Windows drive/UNC prefix ('C:\...')
 *  - empty components ('a//b', trailing slash on a file)
 *  - '.' / '..' components — even a non-escaping '..' is refused so the rule
 *    is simple and auditable rather than "resolved and maybe forgiven"
 *
 * @param {unknown} rawPath
 * @param {PathRules} [rules]
 * @returns {{ok: true, components: string[], normalizedPath: string} | {ok: false, reason: string}}
 */
export function checkEntryPath(rawPath, rules = DEFAULT_PATH_RULES) {
  if (typeof rawPath !== 'string' || rawPath.length === 0) {
    return { ok: false, reason: 'PATH_EMPTY_OR_NON_STRING' };
  }
  if (rawPath.includes('\0')) {
    return { ok: false, reason: 'PATH_CONTAINS_NUL' };
  }
  if (rawPath.includes('\\')) {
    return { ok: false, reason: 'PATH_CONTAINS_BACKSLASH' };
  }
  if (rawPath.startsWith(rules.separator)) {
    return { ok: false, reason: 'PATH_ABSOLUTE' };
  }
  // Windows drive prefix (C:/...) or UNC/rooted prefix (//server/...).
  if (/^[A-Za-z]:[\/]/.test(rawPath) || /^\/\//.test(rawPath)) {
    return { ok: false, reason: 'PATH_ABSOLUTE' };
  }

  const rawComponents = rawPath.split(rules.separator);
  if (rawComponents.some((c) => c.length === 0)) {
    return { ok: false, reason: 'PATH_EMPTY_COMPONENT' };
  }
  if (rawComponents.includes(RESERVED_NAME)) {
    return { ok: false, reason: 'PATH_DOT_COMPONENT' };
  }
  if (rawComponents.includes(PARENT_NAME)) {
    return { ok: false, reason: 'PATH_PARENT_COMPONENT' };
  }

  const components = rawComponents.map(normalizeComponent);
  return { ok: true, components, normalizedPath: components.join(rules.separator) };
}

/**
 * Explicit root-containment assertion. Structural checks already make escape
 * impossible, but containment is asserted again on the *computed* target so
 * the security boundary does not depend solely on upstream parsing — this is
 * the "confirm every entry belongs to its isolated directory" gate.
 *
 * @param {readonly string[]} targetComponents NFC-normalized relative target
 * @param {PathRules} [rules]
 * @returns {boolean}
 */
export function isInsideRoot(targetComponents, rules = DEFAULT_PATH_RULES) {
  if (!Array.isArray(targetComponents) || targetComponents.length === 0) {
    return false;
  }
  return targetComponents.every(
    (c) =>
      typeof c === 'string' &&
      c.length > 0 &&
      c !== RESERVED_NAME &&
      c !== PARENT_NAME &&
      !c.includes(rules.separator) &&
      !c.includes('\\') &&
      !c.includes('\0'),
  );
}
