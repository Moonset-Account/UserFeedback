/**
 * Key/prefix helpers.
 *
 * Keys are plain strings. A subscription is defined by a prefix that matches
 * whole path segments, using `separator` (default `/`):
 *
 *   prefix "app"     -> "app", "app/db/host"       (not "apple")
 *   prefix "app/"    -> same as "app" (trailing separators are ignored)
 *   prefix ""        -> every key
 *
 * A separator of "" switches to raw string-prefix matching, which is handy for
 * flat keys such as "app.db.host".
 */

export const DEFAULT_SEPARATOR = '/';

export function normalizeSeparator(separator) {
  if (separator === undefined || separator === null) return DEFAULT_SEPARATOR;
  if (typeof separator !== 'string') {
    throw new TypeError('options.separator must be a string');
  }
  return separator;
}

export function normalizePrefix(prefix, separator = DEFAULT_SEPARATOR) {
  if (prefix === undefined || prefix === null) return '';
  if (typeof prefix !== 'string') {
    throw new TypeError('prefix must be a string');
  }
  if (prefix === '') return '';

  // With raw string matching there is nothing to strip, and `endsWith('')`
  // would otherwise loop forever.
  if (separator === '') return prefix;

  let normalized = prefix;
  while (normalized.length >= separator.length && normalized.endsWith(separator)) {
    normalized = normalized.slice(0, -separator.length);
  }
  return normalized;
}

export function matchesPrefix(normalizedPrefix, key, separator = DEFAULT_SEPARATOR) {
  if (normalizedPrefix === '') return true;
  if (separator === '') return key.startsWith(normalizedPrefix);
  return key === normalizedPrefix || key.startsWith(normalizedPrefix + separator);
}

export function assertKey(key) {
  if (typeof key !== 'string' || key.length === 0) {
    throw new TypeError('change.key must be a non-empty string');
  }
  return key;
}
