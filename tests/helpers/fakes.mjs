'use strict';

import { collectSource } from '../../src/body-source.js';

/**
 * Scripted transport: no real I/O. A route is (transportRequest, record) =>
 * response. The transport always confirms one written byte (when a body is
 * present) before answering, as required by the acceptance scenario.
 *
 * Each record captures attempt number, method, url, request headers, the
 * number of body bytes actually written, and the collected body text — so
 * tests can observe exactly which identity was used on the second attempt
 * and whether the content source was reopened.
 */
export function createScriptedTransport(routes, { options = {} } = {}) {
  const calls = [];

  async function transport(transportRequest) {
    const { chunks, bytes } = transportRequest.body
      ? await collectSource(transportRequest.body)
      : { chunks: [], bytes: 0 };

    // The contract of the acceptance double: the fake transport confirms one
    // byte was written before it surfaces the challenge.
    const bytesSent = bytes > 0 ? Math.min(1, bytes) : 0;

    const record = {
      attempt: transportRequest.attempt,
      method: transportRequest.method,
      url: transportRequest.url,
      headers: { ...transportRequest.headers },
      bytesSent,
      bodyText: new TextDecoder().decode(concat(chunks)),
      bodyChunks: chunks,
    };
    calls.push(record);

    const route = routes[calls.length - 1];
    if (typeof route !== 'function') {
      throw new Error(`scripted transport: no scripted response for call #${calls.length}`);
    }
    const response = await route(transportRequest, record);
    record.responseStatus = response.status;
    record.responseHeaders = { ...(response.headers ?? {}) };
    return { ...response, bytesSent };
  }

  transport.calls = calls;
  transport.options = options;
  return transport;
}

function concat(chunks) {
  const total = chunks.reduce((n, c) => n + c.byteLength, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.byteLength;
  }
  return out;
}

export function okResponse(body = { ok: true }, headers = {}) {
  return { status: 200, headers, body };
}

export function challengeResponse(challengeId, { realm = 'api', scheme = 'Bearer', extraHeaders = {} } = {}) {
  return {
    status: 401,
    headers: {
      'www-authenticate': `${scheme} realm="${realm}", challenge_id="${challengeId}"`,
      ...extraHeaders,
    },
  };
}

export function redirectResponse(location, status = 307, extraHeaders = {}) {
  return { status, headers: { location, ...extraHeaders } };
}

/**
 * In-memory credential registry acting as the pluggable credential lookup.
 * Credentials are keyed by `${origin}|${realm}` and carry an identity so
 * tests can observe which credential was selected.
 */
export function createCredentialRegistry(entries = []) {
  const table = new Map();
  const lookups = [];
  for (const entry of entries) {
    table.set(`${entry.origin}|${entry.realm}`, entry);
  }

  async function provider({ origin, realm }) {
    lookups.push({ origin, realm: realm ?? null });
    if (!realm) return null;
    return table.get(`${origin}|${realm}`) ?? null;
  }

  provider.lookups = lookups;
  provider.put = (entry) => {
    table.set(`${entry.origin}|${entry.realm}`, entry);
  };
  return provider;
}

export function credential(id, origin, realm) {
  return {
    id,
    origin,
    realm,
    authorization: `Bearer token-for-${id}`,
  };
}

/** Flatten the client event log to event types in order. */
export function eventTypes(client) {
  return client.events.map((e) => e.type);
}

/** Find the last event of a given type. */
export function lastEvent(client, type) {
  for (let i = client.events.length - 1; i >= 0; i -= 1) {
    if (client.events[i].type === type) return client.events[i];
  }
  return undefined;
}
