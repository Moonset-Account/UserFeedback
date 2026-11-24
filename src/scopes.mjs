/**
 * Site-scope normalization and containment checks.
 *
 * The review model accepts conventional extension match patterns
 * (`*://*.example.com/path/*`, `<all_urls>`), bare hosts, and URL prefixes.
 * The functions in this module are intentionally dependency-free.
 */

const WEB_WILDCARD_SCHEMES = new Set(['http', 'https', 'ws', 'wss']);

/**
 * @typedef {object} SiteScope
 * @property {string} raw
 * @property {string} canonical
 * @property {string} scheme
 * @property {string} host
 * @property {string} hostname
 * @property {string|null} port
 * @property {string} path
 * @property {boolean} allUrls
 */

export class SitePatternError extends Error {
  constructor(pattern, detail) {
    super(`Invalid site scope ${JSON.stringify(pattern)}: ${detail}`);
    this.name = 'SitePatternError';
    this.pattern = pattern;
    this.code = 'INVALID_SITE_SCOPE';
  }
}

function splitHostPort(host) {
  // IPv6 is uncommon in extension scopes; keep its brackets intact.
  if (host.startsWith('[')) {
    const end = host.indexOf(']');
    if (end === -1) return { hostname: host, port: null };
    const rest = host.slice(end + 1);
    return {
      hostname: host.slice(0, end + 1),
      port: rest.startsWith(':') ? rest.slice(1) : null,
    };
  }
  const index = host.lastIndexOf(':');
  if (index === -1) return { hostname: host, port: null };
  const maybePort = host.slice(index + 1);
  if (/^\d+$/.test(maybePort)) {
    return { hostname: host.slice(0, index), port: maybePort };
  }
  return { hostname: host, port: null };
}

/**
 * Normalize a site scope into its scheme/host/path parts.
 * @param {unknown} value
 * @returns {SiteScope}
 */
export function normalizeSiteScope(value) {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    value = value.pattern ?? value.scope ?? value.url ?? value.matchPattern ?? value.host;
  }
  if (typeof value !== 'string' || value.trim() === '') {
    throw new SitePatternError(String(value), 'must be a non-empty string');
  }

  const raw = value.trim();
  if (raw === '<all_urls>' || raw === '*' || raw === '*://*/*') {
    return {
      raw,
      canonical: '<all_urls>',
      scheme: '*',
      host: '*',
      hostname: '*',
      port: null,
      path: '/*',
      allUrls: true,
    };
  }

  let scheme = '*';
  let rest = raw;
  const schemeMatch = /^([a-zA-Z][a-zA-Z0-9+.-]*|\*):\/\//.exec(raw);
  if (schemeMatch) {
    scheme = schemeMatch[1].toLowerCase();
    rest = raw.slice(schemeMatch[0].length);
  }

  const slashIndex = rest.indexOf('/');
  let hostPart;
  let path;
  if (slashIndex === -1) {
    hostPart = rest;
    // A bare origin/host is treated as an origin-wide scope.
    path = '/*';
  } else {
    hostPart = rest.slice(0, slashIndex);
    path = rest.slice(slashIndex);
  }

  if (!hostPart && scheme !== 'file') throw new SitePatternError(raw, 'missing host');
  if (path === '') path = '/*';

  // Bare values such as example.com/api are already handled by the slash above.
  // Values such as "example.com" have no URL scheme; leave scheme as '*'.
  const { hostname, port } = splitHostPort(hostPart.toLowerCase());
  if ((!hostname && scheme !== 'file') || /\s/.test(hostname)) {
    throw new SitePatternError(raw, 'invalid host');
  }

  // A path prefix without any wildcard is interpreted as that prefix plus all
  // sub-paths. Exact file scopes should contain a literal file and may be passed
  // through by callers via an object with exact:true in future schemas; common
  // release-review inputs use wildcard suffixes explicitly.
  const canonicalPath = path;
  const canonicalHost = port ? `${hostname}:${port}` : hostname;
  const canonical = `${scheme}://${canonicalHost}${canonicalPath}`;

  return {
    raw,
    canonical,
    scheme,
    host: canonicalHost,
    hostname,
    port,
    path: canonicalPath,
    allUrls: false,
  };
}

function schemeCovers(approved, requested) {
  if (approved === requested) return true;
  if (approved === '*') return WEB_WILDCARD_SCHEMES.has(requested);
  return false;
}

function hostCovers(approvedScope, requestedScope) {
  const approved = approvedScope.hostname;
  const requested = requestedScope.hostname;

  if (approved === '*') return true;
  if (approved.startsWith('*.')) {
    const suffix = approved.slice(2);
    return requested === suffix || requested.endsWith(`.${suffix}`);
  }
  if (approved !== requested) return false;

  // An approval without an explicit port is origin-default only. Do not let an
  // arbitrary non-default port slip through.
  if (approvedScope.port === null) return requestedScope.port === null;
  return approvedScope.port === requestedScope.port;
}

function escapeRegExp(text) {
  return text.replace(/[.?+^${}()|[\]\\]/g, '\\$&');
}

/**
 * Convert a star-only scope glob to an anchored regular expression.
 * @param {string} glob
 */
export function globToRegExp(glob) {
  let re = '^';
  for (const char of glob) {
    if (char === '*') re += '.*';
    else re += escapeRegExp(char);
  }
  return new RegExp(`${re}$`, 's');
}

function pathCovers(approvedPath, requestedPath) {
  if (approvedPath === requestedPath) return true;
  if (approvedPath === '/*' || approvedPath === '*') return true;

  // Exact/literal requested paths can be tested directly.
  if (!requestedPath.includes('*')) {
    return globToRegExp(approvedPath).test(requestedPath);
  }

  // Common prefix-scope case: approved and requested both use a trailing star.
  if (approvedPath.endsWith('*') && requestedPath.endsWith('*')) {
    const approvedPrefix = approvedPath.slice(0, -1);
    const requestedPrefix = requestedPath.slice(0, -1);

    // If approval has no interior wildcard and its literal prefix is a prefix
    // of the requested literal prefix, every requested path is covered.
    if (!approvedPrefix.includes('*') && requestedPrefix.startsWith(approvedPrefix)) {
      return true;
    }
  }

  // If the request is broader than a trailing-prefix shape, conservatively try
  // matching its literal text. This handles identical complex patterns only.
  return globToRegExp(approvedPath).test(requestedPath);
}

/**
 * Return true when an approved scope covers a requested scope.
 * @param {unknown} approvedPattern
 * @param {unknown} requestedPattern
 */
export function siteCovers(approvedPattern, requestedPattern) {
  const approved = normalizeSiteScope(approvedPattern);
  const requested = normalizeSiteScope(requestedPattern);
  return (
    schemeCovers(approved.scheme, requested.scheme) &&
    hostCovers(approved, requested) &&
    pathCovers(approved.path, requested.path)
  );
}

/**
 * Find all approved scopes that cover the request.
 * @param {readonly unknown[]} approvedPatterns
 * @param {unknown} requestedPattern
 */
export function findCoveringScopes(approvedPatterns, requestedPattern) {
  /** @type {string[]} */
  const covers = [];
  for (const approved of approvedPatterns ?? []) {
    try {
      const normalized = normalizeSiteScope(approved);
      if (siteCovers(normalized, requestedPattern)) covers.push(normalized.canonical);
    } catch {
      // Invalid approvals are reported during approval normalization.
    }
  }
  return covers;
}
