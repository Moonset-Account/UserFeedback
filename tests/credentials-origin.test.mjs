'use strict';

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { AuthChallengeClient } from '../src/auth-client.js';
import {
  createReopenableSource,
  createOneShotSource,
} from '../src/body-source.js';
import {
  CredentialMismatchError,
  NoCredentialAvailableError,
  ReconfirmRequiredError,
} from '../src/errors.js';
import {
  createScriptedTransport,
  createCredentialRegistry,
  challengeResponse,
  redirectResponse,
  okResponse,
  lastEvent,
  credential,
} from './helpers/fakes.mjs';

const ORIGIN_A = 'https://api.internal.example';
const ORIGIN_B = 'https://eu.internal.example';

function twoOriginRegistry() {
  return createCredentialRegistry([
    credential('svc-a', ORIGIN_A, 'api'),
    credential('svc-b', ORIGIN_B, 'api'),
  ]);
}

test('ACCEPT: credential must match the challenged origin AND realm; realm from challenge drives lookup', async () => {
  const transport = createScriptedTransport([
    () => challengeResponse('c-1', { realm: 'billing' }),
    () => okResponse(),
  ]);
  const provider = createCredentialRegistry([
    credential('svc-a', ORIGIN_A, 'api'),
    credential('svc-billing', ORIGIN_A, 'billing'),
  ]);
  const client = new AuthChallengeClient({ transport, credentialProvider: provider });

  const response = await client.send({ method: 'GET', url: `${ORIGIN_A}/v1/invoices` });

  assert.equal(response.status, 200);
  assert.equal(response.identity, 'svc-billing');
  assert.deepEqual(provider.lookups[0], { origin: ORIGIN_A, realm: 'billing' });
  assert.equal(transport.calls[1].headers.authorization, 'Bearer token-for-svc-billing');
});

test('REJECT: credential bound to another origin is refused even if the provider hands it over', async () => {
  const transport = createScriptedTransport([
    () => challengeResponse('c-1'),
  ]);
  // Malicious/buggy provider returns a B-origin credential for an A-origin request.
  const provider = async () => credential('svc-b', ORIGIN_B, 'api');
  const client = new AuthChallengeClient({ transport, credentialProvider: provider });

  await assert.rejects(
    client.send({ method: 'GET', url: `${ORIGIN_A}/v1/orders` }),
    (err) => {
      assert.ok(err instanceof CredentialMismatchError);
      assert.equal(err.details.expectedOrigin, ORIGIN_A);
      assert.equal(err.details.actualOrigin, ORIGIN_B);
      return true;
    },
  );
  assert.equal(transport.calls.length, 1, 'mismatched credential must never be put on the wire');
});

test('REJECT: credential for the wrong realm is refused', async () => {
  const transport = createScriptedTransport([
    () => challengeResponse('c-1', { realm: 'billing' }),
  ]);
  const provider = async () => credential('svc-a', ORIGIN_A, 'api');
  const client = new AuthChallengeClient({ transport, credentialProvider: provider });

  await assert.rejects(
    client.send({ method: 'GET', url: `${ORIGIN_A}/v1/invoices` }),
    (err) => err instanceof CredentialMismatchError && err.details.expectedRealm === 'billing',
  );
});

test('REJECT: provider returning null for a challenge means the challenge cannot be answered', async () => {
  const transport = createScriptedTransport([() => challengeResponse('c-1')]);
  const client = new AuthChallengeClient({ transport, credentialProvider: async () => null });

  await assert.rejects(
    client.send({ method: 'GET', url: `${ORIGIN_A}/v1/orders` }),
    (err) => err instanceof NoCredentialAvailableError,
  );
  assert.equal(transport.calls.length, 1);
});

test('ACCEPT: cross-origin redirect drops the old identity and selects the credential for the new origin', async () => {
  const KEY = 'idem-99';
  const transport = createScriptedTransport([
    // Origin A: one byte confirmed, then 307 to origin B, and acknowledge the key.
    () =>
      redirectResponse(`${ORIGIN_B}/v1/mirror`, 307, {
        'x-idempotency-acknowledged': KEY,
      }),
    // Origin B then challenges with its own challenge id.
    () => challengeResponse('c-b-1'),
    () => okResponse({ mirrored: true }),
  ]);
  const provider = twoOriginRegistry();
  const client = new AuthChallengeClient({ transport, credentialProvider: provider });

  const body = createReopenableSource('mirror-me');
  body.idempotencyKey = KEY;

  const response = await client.send({
    method: 'POST',
    url: `${ORIGIN_A}/v1/handoff`,
    body,
    realm: 'api',
  });

  assert.equal(response.status, 200);
  assert.equal(response.identity, 'svc-b');
  assert.equal(transport.calls.length, 3);

  // First attempt used the A-origin credential; it must not travel to origin B.
  assert.equal(transport.calls[0].headers.authorization, 'Bearer token-for-svc-a');
  assert.equal(transport.calls[1].url.startsWith(ORIGIN_B), true);
  // Cross-origin reselection (realm already known) immediately uses the B identity;
  // it is never the A token.
  assert.notEqual(transport.calls[1].headers.authorization, 'Bearer token-for-svc-a');
  assert.equal(transport.calls[1].headers.authorization, 'Bearer token-for-svc-b');
  // Final attempt keeps the B-origin credential.
  assert.equal(transport.calls[2].headers.authorization, 'Bearer token-for-svc-b');

  // Body was reopened for both B-origin attempts and replayed in full every time.
  assert.equal(body.openCount, 3);
  assert.deepEqual(transport.calls.map((c) => c.bodyText), ['mirror-me', 'mirror-me', 'mirror-me']);

  const changed = lastEvent(client, 'origin-changed');
  assert.equal(changed.fromOrigin, ORIGIN_A);
  assert.equal(changed.toOrigin, ORIGIN_B);
  assert.equal(changed.previousIdentity, 'svc-a');
  // Fresh challenge id space per origin does not matter here, but the
  // provider was queried again against the new origin.
  assert.ok(provider.lookups.some((l) => l.origin === ORIGIN_B && l.realm === 'api'));
});

test('ACCEPT: cross-origin redirect without a realm hint arrives anonymous, then the new origin challenge selects its credential', async () => {
  const KEY = 'idem-anon-99';
  const transport = createScriptedTransport([
    () => redirectResponse(`${ORIGIN_B}/v1/mirror`, 307, { 'x-idempotency-acknowledged': KEY }),
    () => challengeResponse('c-b-anon'),
    () => okResponse({ mirrored: true }),
  ]);
  const provider = twoOriginRegistry();
  const client = new AuthChallengeClient({ transport, credentialProvider: provider });

  const body = createReopenableSource('mirror-me');
  body.idempotencyKey = KEY;

  // No realm hint and no initial credential.
  const response = await client.send({ method: 'POST', url: `${ORIGIN_A}/v1/handoff`, body });

  assert.equal(response.status, 200);
  assert.equal(response.identity, 'svc-b');
  // The redirected attempt at origin B goes out with no Authorization header...
  assert.equal(transport.calls[1].headers.authorization, undefined);
  // ...and the B-origin challenge triggers selection against origin B.
  assert.equal(transport.calls[2].headers.authorization, 'Bearer token-for-svc-b');
  assert.deepEqual(provider.lookups[0], { origin: ORIGIN_B, realm: 'api' });
});

test('REJECT: cross-origin 307 after one byte with a one-shot body stops for reconfirm and leaks no identity', async () => {
  const transport = createScriptedTransport([
    () => redirectResponse(`${ORIGIN_B}/v1/mirror`, 307),
  ]);
  const client = new AuthChallengeClient({
    transport,
    credentialProvider: twoOriginRegistry(),
  });
  const body = createOneShotSource('no-replay');

  await assert.rejects(
    client.send({
      method: 'POST',
      url: `${ORIGIN_A}/v1/handoff`,
      body,
      realm: 'api',
    }),
    (err) => err instanceof ReconfirmRequiredError && err.details.reason === 'body-not-reopenable',
  );
  assert.equal(transport.calls.length, 1, 'no request must be sent to the new origin');
  assert.equal(body.openCount, 1);
});

test('BOUNDARY: 303 redirect converts to GET, drops the body and removes the idempotency header', async () => {
  const KEY = 'idem-303';
  const transport = createScriptedTransport([
    () => redirectResponse(`${ORIGIN_B}/v1/resource`, 303),
    () => okResponse({ seen: true }),
  ]);
  const client = new AuthChallengeClient({
    transport,
    credentialProvider: twoOriginRegistry(),
  });
  const body = createReopenableSource('will-be-dropped');
  body.idempotencyKey = KEY;

  const response = await client.send({
    method: 'POST',
    url: `${ORIGIN_A}/v1/things`,
    body,
    realm: 'api',
  });

  assert.equal(response.status, 200);
  assert.equal(transport.calls[1].method, 'GET');
  assert.equal(transport.calls[1].bodyText, '');
  assert.equal(transport.calls[1].headers['idempotency-key'], undefined);
  assert.equal(body.openCount, 1, 'dropped body is never reopened');
});

test('BOUNDARY: same-origin redirect keeps the negotiated identity for the retry', async () => {
  const KEY = 'idem-same';
  const transport = createScriptedTransport([
    () =>
      redirectResponse(`${ORIGIN_A}/v2/orders`, 307, {
        'x-idempotency-acknowledged': KEY,
      }),
    () => challengeResponse('c-1'),
    () => okResponse(),
  ]);
  const provider = twoOriginRegistry();
  const client = new AuthChallengeClient({ transport, credentialProvider: provider });
  const body = createReopenableSource('same-origin');
  body.idempotencyKey = KEY;

  const response = await client.send({
    method: 'POST',
    url: `${ORIGIN_A}/v1/orders`,
    body,
    realm: 'api',
  });

  assert.equal(response.status, 200);
  assert.equal(transport.calls[1].url, `${ORIGIN_A}/v2/orders`);
  // Same origin: the original credential remains attached.
  assert.equal(transport.calls[1].headers.authorization, 'Bearer token-for-svc-a');
  assert.equal(lastEvent(client, 'origin-changed'), undefined);
});
