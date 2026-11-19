'use strict';

import { MalformedChallengeError } from './errors.js';

/**
 * Parse the value of a WWW-Authenticate response header.
 *
 * Supports the token68/parameterized form used by Bearer/Digest/etc., e.g.
 *   Bearer realm="api", challenge_id="c-1", error="invalid_token"
 *
 * Returns: { scheme, realm, challengeId, params } where params is a map of
 * every raw auth-param (lower-cased key -> string value).
 *
 * A 401 without a parseable scheme, or whose scheme does not carry a
 * challenge_id auth-param, is treated as a malformed challenge because the
 * "negotiate each challenge id at most once" rule needs a stable id.
 */
export function parseChallenge(headerValue) {
  if (typeof headerValue !== 'string' || headerValue.trim() === '') {
    throw new MalformedChallengeError('401 response is missing a WWW-Authenticate header', {
      header: headerValue,
    });
  }

  // Multiple challenges can be comma separated at the top level, but each
  // challenge starts with a scheme token followed by spaces (auth-params use
  // '='). We only need the first challenge here.
  const header = headerValue.trim();
  const sp = header.search(/\s/);
  const scheme = (sp === -1 ? header : header.slice(0, sp)).toLowerCase();
  if (!/^[a-z0-9!#$%&'*+.^_`|~-]+$/.test(scheme)) {
    throw new MalformedChallengeError('WWW-Authenticate header has no valid auth-scheme', {
      header: headerValue,
    });
  }

  const paramPart = sp === -1 ? '' : header.slice(sp + 1).trim();
  const params = parseAuthParams(paramPart);

  const challengeId = params.challenge_id;
  if (challengeId === undefined || challengeId === '') {
    throw new MalformedChallengeError(
      `WWW-Authenticate ${scheme} challenge does not provide a challenge_id`,
      { scheme, header: headerValue },
    );
  }

  return {
    scheme,
    realm: params.realm,
    challengeId,
    params,
  };
}

/**
 * Parse comma separated key="quoted" / key=token auth-params.
 * Handles quoted-string backslash escapes per RFC 7235.
 */
function parseAuthParams(input) {
  const params = {};
  let i = 0;
  const n = input.length;

  while (i < n) {
    // skip leading separators / whitespace
    while (i < n && /[,\s]/.test(input[i])) i += 1;
    if (i >= n) break;

    // key
    const keyStart = i;
    while (i < n && /[a-zA-Z0-9!#$%&'*+.^_`|~-]/.test(input[i])) i += 1;
    const key = input.slice(keyStart, i).toLowerCase();
    while (i < n && /\s/.test(input[i])) i += 1;
    if (input[i] !== '=') {
      // token68 blob (e.g. Basic abc==) — stop parsing auth-params.
      break;
    }
    i += 1;
    while (i < n && /\s/.test(input[i])) i += 1;

    let value;
    if (input[i] === '"') {
      i += 1;
      let raw = '';
      while (i < n && input[i] !== '"') {
        if (input[i] === '\\' && i + 1 < n) {
          raw += input[i + 1];
          i += 2;
        } else {
          raw += input[i];
          i += 1;
        }
      }
      if (i >= n) {
        throw new MalformedChallengeError('unterminated quoted string in WWW-Authenticate', {
          header: input,
        });
      }
      i += 1; // closing quote
      value = raw;
    } else {
      const valStart = i;
      while (i < n && input[i] !== ',') i += 1;
      value = input.slice(valStart, i).trim();
    }

    if (key) params[key] = value;
  }

  return params;
}
