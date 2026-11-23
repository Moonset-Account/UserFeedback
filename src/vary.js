/**
 * Vary header handling.
 *
 * A variant index is derived from the request headers named by the Vary
 * field. Header names are compared case-insensitively; absent headers
 * contribute an empty value so that "header missing" and "header present
 * but empty" remain distinguishable only through value handling.
 */

/**
 * Normalize any of the supported header representations into an array of
 * [lowercase-name, value] pairs.
 *
 * @param {Headers|Map<string,string>|Record<string,string>|Array<[string,string]>|undefined} headers
 * @returns {Array<[string, string]>}
 */
export function normalizeHeaders(headers) {
  if (headers === undefined || headers === null) return [];
  if (typeof Headers !== 'undefined' && headers instanceof Headers) {
    return [...headers.entries()].map(([name, value]) => [name.toLowerCase(), String(value)]);
  }
  if (headers instanceof Map) {
    return [...headers.entries()].map(([name, value]) => [String(name).toLowerCase(), String(value)]);
  }
  if (Array.isArray(headers)) {
    return headers.map(([name, value]) => [String(name).toLowerCase(), String(value)]);
  }
  if (typeof headers === 'object') {
    return Object.entries(headers).map(([name, value]) => [name.toLowerCase(), String(value)]);
  }
  return [];
}

/**
 * Parse a Vary header value into a sorted list of lowercase field names.
 * `Vary: *` yields a single wildcard token.
 *
 * @param {string|string[]|undefined} raw
 * @returns {string[]}
 */
export function parseVary(raw) {
  if (raw === undefined || raw === null) return [];
  const tokens = (Array.isArray(raw) ? raw.join(',') : String(raw))
    .split(',')
    .map((token) => token.trim().toLowerCase())
    .filter((token) => token.length > 0);
  if (tokens.includes('*')) return ['*'];
  return [...new Set(tokens)].sort();
}

/**
 * Build a stable variant key for a request and a Vary field list.
 *
 * @param {string[]} varyFields parsed Vary fields
 * @param {Headers|Map|Record|Array|undefined} requestHeaders
 * @returns {string}
 */
export function buildVariantKey(varyFields, requestHeaders) {
  if (varyFields.includes('*')) {
    // Wildcard Vary means every request is its own variant.
    return JSON.stringify(['*', JSON.stringify(normalizeHeaders(requestHeaders))]);
  }
  const pairs = normalizeHeaders(requestHeaders);
  const lookup = new Map();
  for (const [name, value] of pairs) {
    if (!lookup.has(name)) lookup.set(name, []);
    lookup.get(name).push(value);
  }
  const parts = varyFields.map((field) => {
    const values = lookup.get(field);
    return [field, values ? values.join(',') : null];
  });
  return JSON.stringify(parts);
}

/**
 * Compare two parsed Vary field lists for equality.
 */
export function varyEquals(a, b) {
  if (a.length !== b.length) return false;
  return a.every((field, index) => field === b[index]);
}
