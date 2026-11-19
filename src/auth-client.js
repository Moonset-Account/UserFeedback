'use strict';

import { parseChallenge } from './challenge.js';
import {
  AuthClientError,
  ChallengeAlreadyNegotiatedError,
  CredentialMismatchError,
  NoCredentialAvailableError,
  ReconfirmRequiredError,
  RedirectError,
} from './errors.js';

const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);
const DEFAULT_IDEMPOTENCY_HEADER = 'idempotency-key';
const DEFAULT_IDEMPOTENCY_ACK_HEADER = 'x-idempotency-acknowledged';

function originOf(url) {
  return `${url.protocol}//${url.host}`;
}

function normalizeHeaders(headers) {
  const out = {};
  if (!headers) return out;
  for (const [key, value] of Object.entries(headers)) {
    out[key.toLowerCase()] = String(value);
  }
  return out;
}

function getHeader(headers, name) {
  if (!headers) return undefined;
  const target = name.toLowerCase();
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() === target) return value;
  }
  return undefined;
}

/**
 * AuthChallengeClient
 *
 * Pluggable pieces (both supplied by the caller, no network code lives here):
 *
 *  - transport(transportRequest): async, returns
 *      { status, headers, bytesSent, body? }. `bytesSent` is the number of
 *      request-body bytes the transport actually wrote before answering; the
 *      client's resend safety decision depends on it. The transport receives
 *      an already-opened async iterable as `body` (or null).
 *
 *  - credentialProvider({ origin, realm, challenge, request }): async,
 *      returns a credential { id, origin, realm, authorization } or null.
 *      The returned credential MUST match the requested origin and realm;
 *      the client rejects mismatches itself as defense in depth.
 *
 * Guarantees implemented here:
 *  - credentials are always selected for the request's current origin and
 *    authentication realm; a cross-origin redirect drops the old identity
 *    and triggers a fresh selection;
 *  - each challenge id is negotiated at most once per request chain;
 *  - retries before any body byte was sent simply attach credentials;
 *  - after body bytes were sent, resend requires BOTH a reopenable content
 *    source (body.reopenable === true) and a server-acknowledged idempotency
 *    key (body.idempotencyKey + matching ack header), otherwise the client
 *    stops with ReconfirmRequiredError and never replays the body.
 */
export class AuthChallengeClient {
  constructor({
    transport,
    credentialProvider,
    maxRedirects = 5,
    idempotencyHeader = DEFAULT_IDEMPOTENCY_HEADER,
    idempotencyAckHeader = DEFAULT_IDEMPOTENCY_ACK_HEADER,
    onEvent = null,
  } = {}) {
    if (typeof transport !== 'function') {
      throw new TypeError('AuthChallengeClient requires a transport function');
    }
    if (typeof credentialProvider !== 'function') {
      throw new TypeError('AuthChallengeClient requires a credentialProvider function');
    }
    this.transport = transport;
    this.credentialProvider = credentialProvider;
    this.maxRedirects = maxRedirects;
    this.idempotencyHeader = idempotencyHeader.toLowerCase();
    this.idempotencyAckHeader = idempotencyAckHeader.toLowerCase();
    this.onEvent = onEvent;
    this.events = [];
  }

  #emit(event) {
    const recorded = { ...event, at: this.events.length };
    this.events.push(recorded);
    if (typeof this.onEvent === 'function') this.onEvent(recorded);
  }

  /**
   * Send one request through the challenge/redirect state machine.
   *
   * request: {
   *   method?: string, url: string, headers?: object,
   *   body?: content source (see body-source.js) | null,
   *   realm?: string  // optional proactive realm hint
   * }
   *
   * Resolves with the final transport response plus `identity` (credential id
   * used on the accepted attempt, or null) and `attempts`.
   */
  async send(request = {}) {
    if (!request.url) throw new TypeError('request.url is required');

    const negotiated = new Set();
    let method = (request.method ?? 'GET').toString().toUpperCase();
    let url = new URL(request.url);
    let headers = normalizeHeaders(request.headers);
    let body = request.body ?? null;
    let realmHint = request.realm;
    let selectedCredential = null;
    let currentOrigin = originOf(url);
    let redirectCount = 0;
    let attempt = 0;
    // Sticky within this request chain: once a server response has
    // acknowledged the idempotency key for a body-bearing attempt, every
    // later replay of the SAME reopenable source is already authorized
    // (e.g. a redirect followed by a 401 on the replayed body).
    let resendAuthorized = false;

    if (body?.idempotencyKey) {
      headers[this.idempotencyHeader] = body.idempotencyKey;
    }

    // Proactive selection only when the caller already knows the realm.
    if (realmHint) {
      selectedCredential = await this.#trySelectCredential(currentOrigin, realmHint, null);
      if (selectedCredential) headers.authorization = selectedCredential.authorization;
    }

    for (;;) {
      attempt += 1;
      const openedBody = body ? body.open() : null;
      const transportRequest = {
        method,
        url: url.toString(),
        headers: { ...headers },
        body: openedBody,
        hasBody: body !== null,
        byteLength: body ? body.byteLength : 0,
        attempt,
      };

      this.#emit({
        type: 'request',
        attempt,
        method,
        url: url.toString(),
        origin: currentOrigin,
        hasBody: body !== null,
        identity: selectedCredential ? selectedCredential.id : null,
      });

      const response = await this.transport(transportRequest);
      const bytesSent = Number.isFinite(response.bytesSent) ? response.bytesSent : 0;

      this.#emit({
        type: 'response',
        attempt,
        status: response.status,
        bytesSent,
        url: url.toString(),
      });

      if (response.status === 401) {
        await this.#handleChallenge({
          response,
          negotiated,
          resendAuthorized,
          onResendAuthorized: () => {
            resendAuthorized = true;
          },
          getState: () => ({ method, url, headers, body, currentOrigin, realmHint, bytesSent, attempt }),
          applyCredential: (cred) => {
            selectedCredential = cred;
            delete headers.authorization;
            headers.authorization = cred.authorization;
          },
          setRealmHint: (realm) => {
            realmHint = realm;
          },
        });
        continue;
      }

      if (REDIRECT_STATUSES.has(response.status)) {
        const next = this.#handleRedirect({
          response,
          redirectCount,
          resendAuthorized,
          onResendAuthorized: () => {
            resendAuthorized = true;
          },
          state: {
            method,
            url,
            headers,
            body,
            bytesSent,
            currentOrigin,
            realmHint,
            identity: selectedCredential ? selectedCredential.id : null,
          },
        });
        redirectCount += 1;
        method = next.method;
        url = next.url;
        body = next.body;
        headers = next.headers;
        currentOrigin = next.currentOrigin;
        realmHint = next.realmHint;
        if (next.originChanged) {
          // Cross-origin: the old identity was bound to the old origin/realm.
          // Reselect for the new origin when the realm is already known;
          // otherwise stay anonymous until the new origin challenges.
          selectedCredential = null;
          delete headers.authorization;
          if (realmHint) {
            selectedCredential = await this.#trySelectCredential(currentOrigin, realmHint, null);
            if (selectedCredential) headers.authorization = selectedCredential.authorization;
          }
        }
        continue;
      }

      return {
        ...response,
        identity: selectedCredential ? selectedCredential.id : null,
        attempts: attempt,
      };
    }
  }

  async #handleChallenge({
    response,
    negotiated,
    resendAuthorized,
    onResendAuthorized,
    getState,
    applyCredential,
    setRealmHint,
  }) {
    const state = getState();
    const challenge = parseChallenge(getHeader(response.headers, 'www-authenticate'));

    if (negotiated.has(challenge.challengeId)) {
      this.#emit({
        type: 'challenge-rejected',
        reason: 'already-negotiated',
        challengeId: challenge.challengeId,
        attempt: state.attempt,
      });
      throw new ChallengeAlreadyNegotiatedError(challenge.challengeId, {
        url: state.url.toString(),
        attempt: state.attempt,
      });
    }
    negotiated.add(challenge.challengeId);

    this.#emit({
      type: 'challenge',
      attempt: state.attempt,
      challengeId: challenge.challengeId,
      scheme: challenge.scheme,
      realm: challenge.realm ?? state.realmHint ?? null,
      bytesSent: state.bytesSent,
    });

    // Resend safety: once body bytes are on the wire, a retry is only legal
    // with a reopenable source AND a server-acknowledged idempotency key.
    // A prior acknowledgment in this request chain (e.g. on the redirect that
    // led here) keeps the replay authorized for the same source.
    if (state.bytesSent > 0) {
      const reopenable = state.body?.reopenable === true;
      const idempotencyKey = state.body?.idempotencyKey ?? null;
      const ackValue = getHeader(response.headers, this.idempotencyAckHeader);
      const acknowledged =
        idempotencyKey !== null && ackValue !== undefined && String(ackValue).trim() === idempotencyKey;
      const authorized = resendAuthorized || acknowledged;

      if (!reopenable || !authorized) {
        const reason = !reopenable
          ? 'body-not-reopenable'
          : 'idempotency-key-not-acknowledged';
        this.#emit({
          type: 'reconfirm-required',
          attempt: state.attempt,
          reason,
          bytesSent: state.bytesSent,
          reopenable,
          hasIdempotencyKey: idempotencyKey !== null,
          acknowledged,
          url: state.url.toString(),
        });
        throw new ReconfirmRequiredError(
          'request body bytes were already sent; caller must reconfirm before resend ' +
            '(need a reopenable content source and a server-acknowledged idempotency key)',
          {
            reason,
            bytesSent: state.bytesSent,
            reopenable,
            idempotencyKey,
            acknowledged,
            url: state.url.toString(),
            challengeId: challenge.challengeId,
          },
        );
      }

      if (acknowledged) onResendAuthorized();
      this.#emit({
        type: 'body-reopen-authorized',
        attempt: state.attempt,
        challengeId: challenge.challengeId,
        idempotencyKey,
        viaPriorAuthorization: resendAuthorized && !acknowledged,
      });
    }

    const realm = challenge.realm ?? state.realmHint ?? null;
    if (challenge.realm) setRealmHint(challenge.realm);

    const credential = await this.#selectCredential(state.currentOrigin, realm, challenge, {
      method: state.method,
      url: state.url.toString(),
    });
    applyCredential(credential);

    this.#emit({
      type: 'credential-selected',
      identity: credential.id,
      origin: state.currentOrigin,
      realm: credential.realm,
      challengeId: challenge.challengeId,
    });
  }

  #handleRedirect({ response, redirectCount, resendAuthorized, onResendAuthorized, state }) {
    if (redirectCount >= this.maxRedirects) {
      throw new RedirectError(`redirect limit of ${this.maxRedirects} exceeded`, {
        url: state.url.toString(),
      });
    }
    const location = getHeader(response.headers, 'location');
    if (!location) {
      throw new RedirectError('redirect response has no Location header', {
        status: response.status,
        url: state.url.toString(),
      });
    }

    const nextUrl = new URL(location, state.url);
    const nextOrigin = originOf(nextUrl);
    const originChanged = nextOrigin !== state.currentOrigin;

    this.#emit({
      type: 'redirect',
      from: state.url.toString(),
      to: nextUrl.toString(),
      status: response.status,
      originChanged,
      bytesSent: state.bytesSent,
    });

    let { method, body, headers, realmHint } = state;

    if (body) {
      if (response.status === 303) {
        // 303: convert to GET and drop the body entirely.
        method = 'GET';
        body = null;
        headers = { ...headers };
        delete headers['content-length'];
        delete headers['content-type'];
        delete headers[this.idempotencyHeader];
      } else if (state.bytesSent > 0) {
        // 307/308 preserve method+body; bytes already on the wire means the
        // same reconfirm rule applies before the redirected resend. A prior
        // acknowledgment within this request chain stays valid.
        const reopenable = body.reopenable === true;
        const idempotencyKey = body.idempotencyKey ?? null;
        const ackValue = getHeader(response.headers, this.idempotencyAckHeader);
        const acknowledged =
          idempotencyKey !== null && ackValue !== undefined && String(ackValue).trim() === idempotencyKey;
        const authorized = resendAuthorized || acknowledged;
        if (!reopenable || !authorized) {
          this.#emit({
            type: 'reconfirm-required',
            reason: !reopenable ? 'body-not-reopenable' : 'idempotency-key-not-acknowledged',
            bytesSent: state.bytesSent,
            reopenable,
            hasIdempotencyKey: idempotencyKey !== null,
            acknowledged,
            url: state.url.toString(),
          });
          throw new ReconfirmRequiredError(
            'redirect arrived after request body bytes were sent; caller must reconfirm before resend',
            {
              reason: !reopenable ? 'body-not-reopenable' : 'idempotency-key-not-acknowledged',
              bytesSent: state.bytesSent,
              reopenable,
              idempotencyKey,
              acknowledged,
              redirectTo: nextUrl.toString(),
            },
          );
        }
        if (acknowledged) {
          onResendAuthorized();
          this.#emit({
            type: 'body-reopen-authorized',
            idempotencyKey,
            viaPriorAuthorization: false,
          });
        }
      }
    }

    if (originChanged) {
      // The old identity is bound to the old origin/realm and must not travel.
      headers = { ...headers };
      delete headers.authorization;
      this.#emit({
        type: 'origin-changed',
        fromOrigin: state.currentOrigin,
        toOrigin: nextOrigin,
        previousIdentity: state.identity,
      });
    }

    return {
      method,
      url: nextUrl,
      body,
      headers,
      currentOrigin: nextOrigin,
      originChanged,
      realmHint,
    };
  }

  /**
   * Soft selection used for proactive/reselect paths: a provider returning
   * null means "stay anonymous for now", not an error.
   */
  async #trySelectCredential(origin, realm, challenge) {
    const credential = await this.credentialProvider({ origin, realm, challenge, request: {} });
    if (!credential) return null;
    this.#validateCredential(credential, origin, realm);
    return credential;
  }

  /**
   * Strict selection used when a challenge must be answered: provider null
   * is an error because the negotiated challenge cannot proceed.
   */
  async #selectCredential(origin, realm, challenge, request = {}) {
    const credential = await this.credentialProvider({ origin, realm, challenge, request });

    if (!credential) {
      throw new NoCredentialAvailableError(
        `no credential available for origin ${origin}${realm ? ` and realm ${realm}` : ''}`,
        { origin, realm, challengeId: challenge?.challengeId ?? null },
      );
    }
    this.#validateCredential(credential, origin, realm);
    return credential;
  }

  #validateCredential(credential, origin, realm) {
    if (typeof credential.id !== 'string' || typeof credential.authorization !== 'string') {
      throw new NoCredentialAvailableError('credential provider returned an invalid credential', {
        origin,
        realm,
      });
    }
    if (credential.origin !== origin) {
      throw new CredentialMismatchError(
        `credential ${credential.id} is bound to ${credential.origin}, request origin is ${origin}`,
        { expectedOrigin: origin, actualOrigin: credential.origin, credentialId: credential.id },
      );
    }
    if (realm && credential.realm !== realm) {
      throw new CredentialMismatchError(
        `credential ${credential.id} is for realm ${credential.realm}, request realm is ${realm}`,
        { expectedRealm: realm, actualRealm: credential.realm, credentialId: credential.id },
      );
    }
  }
}

export { AuthClientError, getHeader, originOf };
