// CSP3 script enforcement decision library.
//
// Scope (see README "未覆盖边界"):
//   directives : default-src / script-src / script-src-elem (with fallback)
//   sources    : 'self' 'none' 'strict-dynamic', nonce, sha256 hash,
//                HTTPS host-source (wildcard / port / path), https: scheme
// Anything else is reported through `unsupported` instead of being silently
// accepted. Trust propagation is tracked independently per policy.

import crypto from 'node:crypto';

const FALLBACK_CHAIN = ['script-src-elem', 'script-src', 'default-src'];

// Directives named in CSP3 that this library recognises but does not use for
// script decisions. Unknown names are reported as unsupported-directive.
const KNOWN_DIRECTIVES = new Set([
  'default-src', 'script-src', 'script-src-elem',
  'style-src', 'style-src-elem', 'style-src-attr',
  'img-src', 'font-src', 'connect-src', 'object-src', 'child-src',
  'frame-src', 'media-src', 'manifest-src', 'worker-src',
  'base-uri', 'form-action', 'frame-ancestors', 'navigate-to',
  'sandbox', 'report-uri', 'report-to',
  'trusted-types', 'require-trusted-types-for',
]);

export function sha256Base64(text) {
  return crypto.createHash('sha256').update(text, 'utf8').digest('base64');
}

function maskToken(raw) {
  return String(raw).replace(/^('?(?:nonce|sha256)-)[^']*('?)$/i, "$1***$2");
}

// ---------------------------------------------------------------------------
// Source expression classification
// ---------------------------------------------------------------------------

function parseHostBody(rest) {
  const slash = rest.indexOf('/');
  const hostPort = slash === -1 ? rest : rest.slice(0, slash);
  const path = slash === -1 ? null : rest.slice(slash);
  let host = hostPort;
  let port = null;
  const colon = hostPort.lastIndexOf(':');
  if (colon !== -1) {
    const portText = hostPort.slice(colon + 1);
    if (!/^\d+$/.test(portText)) return null;
    port = Number(portText);
    host = hostPort.slice(0, colon);
  }
  let wildcard = false;
  if (host.startsWith('*.')) {
    wildcard = true;
    host = host.slice(2);
  }
  if (!host || !/^[a-z0-9.-]+$/.test(host) || host.includes('..')) return null;
  return { wildcard, host, port, path };
}

export function classifySource(rawToken) {
  const lower = String(rawToken).toLowerCase();

  if (lower.startsWith("'")) {
    if (!lower.endsWith("'") || lower.length < 3) {
      return { unsupported: true, reason: 'malformed-quoted-source' };
    }
    if (lower === "'self'") return { type: 'self' };
    if (lower === "'none'") return { type: 'none' };
    if (lower === "'strict-dynamic'") return { type: 'strict-dynamic' };

    if (lower.startsWith("'nonce-")) {
      const value = String(rawToken).slice(7, -1);
      if (value.length >= 1 && /^[A-Za-z0-9+/=_-]+$/.test(value)) {
        return { type: 'nonce', value };
      }
      return { unsupported: true, reason: 'malformed-nonce' };
    }

    if (lower.startsWith("'sha256-")) {
      const value = String(rawToken).slice(8, -1);
      if (/^[A-Za-z0-9+/]+={0,2}$/.test(value)) {
        const buf = Buffer.from(value, 'base64');
        const normalised = buf.toString('base64').replace(/=+$/, '');
        if (buf.length === 32 && normalised === value.replace(/=+$/, '')) {
          return { type: 'sha256', value };
        }
      }
      return { unsupported: true, reason: 'malformed-sha256' };
    }

    if (/^'sha(?:384|512)-/.test(lower)) {
      return { unsupported: true, reason: 'unsupported-hash-algorithm' };
    }
    return { unsupported: true, reason: 'unsupported-keyword' };
  }

  const schemeOnly = lower.match(/^([a-z][a-z0-9+.-]*):$/);
  if (schemeOnly) {
    if (schemeOnly[1] === 'https') return { type: 'scheme-https' };
    return { unsupported: true, reason: 'unsupported-scheme' };
  }

  if (lower === '*') {
    return { unsupported: true, reason: 'unsupported-wildcard' };
  }

  const explicit = lower.match(/^([a-z][a-z0-9+.-]*):\/\/(.*)$/);
  if (explicit) {
    if (explicit[1] !== 'https') {
      return { unsupported: true, reason: 'non-https-host-scheme' };
    }
    const body = parseHostBody(explicit[2]);
    if (!body) return { unsupported: true, reason: 'malformed-host' };
    return { type: 'host', scheme: 'https', ...body };
  }

  // Bare host-source: scheme is inherited from the document origin.
  const body = parseHostBody(lower);
  if (body) return { type: 'host', scheme: null, ...body };

  return { unsupported: true, reason: 'malformed-source-expression' };
}

// ---------------------------------------------------------------------------
// Policy parsing
// ---------------------------------------------------------------------------

export function parsePolicy(text) {
  const directives = new Map();
  const duplicates = [];
  const unsupported = [];

  for (const chunk of String(text).split(';')) {
    const trimmed = chunk.trim();
    if (!trimmed) continue;

    const parts = trimmed.split(/\s+/);
    const name = parts[0].toLowerCase();
    const rawTokens = parts.slice(1);

    // CSP3: when the same directive appears more than once, the first
    // occurrence is used and every later occurrence is ignored.
    if (directives.has(name)) {
      duplicates.push({ directive: name, ignoredText: trimmed });
      continue;
    }

    if (!KNOWN_DIRECTIVES.has(name)) {
      unsupported.push({ directive: name, token: null, reason: 'unsupported-directive' });
      continue;
    }

    const sources = [];
    if (FALLBACK_CHAIN.includes(name)) {
      for (const token of rawTokens) {
        const classified = classifySource(token);
        if (classified.unsupported) {
          unsupported.push({ directive: name, token: maskToken(token), reason: classified.reason });
        } else {
          sources.push(classified);
        }
      }
    }

    directives.set(name, { name, text: trimmed, sources, rawTokens });
  }

  let effectiveName = null;
  for (const candidate of FALLBACK_CHAIN) {
    if (directives.has(candidate)) {
      effectiveName = candidate;
      break;
    }
  }

  return {
    text: String(text),
    directives,
    duplicates,
    unsupported,
    effectiveName,
    effective: effectiveName ? directives.get(effectiveName) : null,
  };
}

// ---------------------------------------------------------------------------
// Host matching
// ---------------------------------------------------------------------------

function hostSourceMatches(src, url, docOrigin) {
  if (src.type === 'self') {
    return url.origin === docOrigin.origin;
  }
  if (src.type === 'scheme-https') {
    return url.protocol === 'https:';
  }
  if (src.type !== 'host') return false;

  if (src.scheme === 'https') {
    if (url.protocol !== 'https:') return false;
  } else if (url.protocol !== docOrigin.protocol) {
    return false;
  }

  if (src.wildcard) {
    // '*.example.test' matches subdomains only, not the bare domain.
    if (!url.hostname.endsWith('.' + src.host)) return false;
  } else if (url.hostname !== src.host) {
    return false;
  }

  const defaultPort = url.protocol === 'https:' ? 443 : 80;
  const actualPort = url.port ? Number(url.port) : defaultPort;
  const expectedPort = src.port ?? (src.scheme === 'https'
    ? 443
    : (docOrigin.port ? Number(docOrigin.port) : defaultPort));
  if (actualPort !== expectedPort) return false;

  if (src.path !== null && !url.pathname.startsWith(src.path)) return false;

  return true;
}

function describeHostSource(src) {
  if (src.type === 'self') return 'host:self';
  if (src.type === 'scheme-https') return 'scheme:https';
  const scheme = src.scheme ? src.scheme + '://' : '';
  const wild = src.wildcard ? '*.' : '';
  const port = src.port !== null ? ':' + src.port : '';
  return `host:${scheme}${wild}${src.host}${port}${src.path ?? ''}`;
}

// ---------------------------------------------------------------------------
// Per-policy decision
// ---------------------------------------------------------------------------

function decide(script, parsed, docOrigin, trustedSet) {
  const effective = parsed.effective;

  // No script-relevant directive at all: the implicit allowlist applies.
  if (!effective) {
    return { allowed: true, basis: ['implicit-allow:no-script-directive'], reason: null };
  }

  const sources = effective.sources;

  // 1. Nonce match (checked first, works for inline and external scripts;
  //    remains effective even when the source list is 'none').
  if (script.nonce != null && script.nonce !== '') {
    const nonceMatch = sources.find((s) => s.type === 'nonce' && s.value === String(script.nonce));
    if (nonceMatch) {
      return { allowed: true, basis: ['nonce'], reason: null };
    }
  }

  // 2. SHA-256 match (inline scripts only). Padding '=' is optional in CSP
  //    base64 values, so compare with padding stripped on both sides.
  if (script.kind === 'inline' && typeof script.body === 'string') {
    const digest = sha256Base64(script.body).replace(/=+$/, '');
    if (sources.some((s) => s.type === 'sha256' && s.value.replace(/=+$/, '') === digest)) {
      return { allowed: true, basis: ['sha256'], reason: null };
    }
  }

  const hasStrictDynamic = sources.some((s) => s.type === 'strict-dynamic');

  // 3. strict-dynamic: host / self / scheme allowlist is ignored; trust can
  //    only come from nonce/hash above or from a creator trusted under THIS
  //    policy. Parser-inserted scripts can never inherit that trust.
  if (hasStrictDynamic) {
    if (!script.parserInserted && script.creator && trustedSet.has(script.creator)) {
      return {
        allowed: true,
        basis: [`strict-dynamic:creator:${script.creator}`],
        reason: null,
      };
    }
    return {
      allowed: false,
      basis: [],
      reason: script.parserInserted
        ? 'blocked:parser-inserted-and-strict-dynamic-ignores-host-sources'
        : 'blocked:creator-not-trusted-under-this-policy',
    };
  }

  // 4. Conventional allowlist matching.
  if (sources.length === 0) {
    return { allowed: false, basis: [], reason: 'blocked:empty-source-list' };
  }

  // 'none' only has effect when it is the sole source expression.
  const usable = sources.filter((s) => s.type !== 'none');
  if (usable.length === 0) {
    return { allowed: false, basis: [], reason: "blocked:source-list-is-'none'" };
  }

  if (script.kind === 'inline') {
    return { allowed: false, basis: [], reason: 'blocked:inline-script-without-nonce-or-sha256' };
  }

  let url;
  try {
    url = new URL(script.url);
  } catch {
    return { allowed: false, basis: [], reason: 'blocked:malformed-external-url' };
  }

  for (const src of usable) {
    if (hostSourceMatches(src, url, docOrigin)) {
      return { allowed: true, basis: [describeHostSource(src)], reason: null };
    }
  }
  return { allowed: false, basis: [], reason: 'blocked:no-matching-host-source' };
}

// ---------------------------------------------------------------------------
// Top-level evaluation
// ---------------------------------------------------------------------------

function summarisePolicy(policy, parsed) {
  return {
    id: policy.id,
    mode: policy.mode === 'report-only' ? 'report-only' : 'enforce',
    effectiveDirective: parsed.effectiveName,
    effectiveDirectiveText: parsed.effective ? parsed.effective.text : null,
    duplicates: parsed.duplicates.map((d) => ({ directive: d.directive, ignoredText: d.ignoredText })),
    unsupported: parsed.unsupported,
  };
}

/**
 * Evaluate every script against every policy.
 *
 * @param {object} input
 * @param {string} input.origin          Document origin, e.g. https://app.test
 * @param {Array}  input.policies        [{ id, text, mode: 'enforce'|'report-only' }]
 * @param {Array}  input.scripts         Creation-ordered script records:
 *   { id, kind: 'inline'|'external', body?, url?, nonce?,
 *     parserInserted: boolean, creator?: scriptId|null }
 * @returns {object} per-policy parse summary and per-script decisions.
 */
export function evaluate({ origin, policies, scripts }) {
  const docOrigin = new URL(origin);
  const parsedPolicies = policies.map((policy) => {
    const mode = policy.mode === 'report-only' ? 'report-only' : 'enforce';
    return { policy: { ...policy, mode }, parsed: parsePolicy(policy.text) };
  });

  // One independent trust set per policy: trust never crosses policies.
  const trustedSets = parsedPolicies.map(() => new Set());

  const scriptResults = scripts.map((script) => {
    const decisions = [];
    const reports = [];

    parsedPolicies.forEach(({ policy, parsed }, index) => {
      const verdict = decide(script, parsed, docOrigin, trustedSets[index]);

      decisions.push({
        policyId: policy.id,
        mode: policy.mode,
        directive: parsed.effectiveName,
        directiveText: parsed.effective ? parsed.effective.text : null,
        allowed: verdict.allowed,
        basis: verdict.basis,
        reason: verdict.reason,
      });

      if (!verdict.allowed) {
        reports.push({
          policyId: policy.id,
          disposition: policy.mode,
          scriptId: script.id,
          effectiveDirective: parsed.effectiveName,
          reason: verdict.reason,
          blockedReference: script.kind === 'external' ? script.url : `inline-script:${script.id}`,
        });
      }

      if (verdict.allowed) trustedSets[index].add(script.id);
    });

    // Intersection over enforcing policies only. Report-only policies never
    // change the executable status; they only contribute violation reports.
    const enforcing = decisions.filter((d) => d.mode === 'enforce');
    const executes = enforcing.every((d) => d.allowed);

    return {
      scriptId: script.id,
      kind: script.kind,
      executes,
      decisions,
      reports,
    };
  });

  return {
    origin,
    policies: parsedPolicies.map(({ policy, parsed }) => summarisePolicy(policy, parsed)),
    scripts: scriptResults,
  };
}
