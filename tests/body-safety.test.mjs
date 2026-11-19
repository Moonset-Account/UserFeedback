'use strict';

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { AuthChallengeClient } from '../src/auth-client.js';
import { createReopenableSource, createOneShotSource, createEmptySource } from '../src/body-source.js';
import { ReconfirmRequiredError } from '../src/errors.js';
import {
  createScriptedTransport,
  createCredentialRegistry,
  challengeResponse,
  okResponse,
  eventTypes,
  lastEvent,
  credential,
} from './helpers/fakes.mjs';

const ORIGIN = 'https://api.internal.example';
const URL = `${ORIGIN}/v1/orders`;
const KEY = 'idem-7788';

function registry() {
  return createCredentialRegistry([credential('svc-a', ORIGIN, 'api')]);
}

test('ACCEPT: one byte confirmed then challenge; reopenable source + acknowledged key is replayed with a fresh identity', async () => {
  const transport = createScriptedTransport([
    () => challengeResponse('c-1', { extraHeaders: { 'x-idempotency-acknowledged': KEY } }),
    (req) => {
      assert.equal(req.headers['authorization'], 'Bearer token-for-svc-a', 'retry must carry selected credential');
      assert.equal(req.headers['idempotency-key'], KEY);
      return okResponse({ orderId: 9 });
    },
  ]);
  const provider = registry();
  const client = new AuthChallengeClient({ transport, credentialProvider: provider });

  const body = createReopenableSource(['pay', 'load']);
  body.idempotencyKey = KEY;

  const response = await client.send({ method: 'POST', url: URL, body });

  assert.equal(response.status, 200);
  assert.equal(response.identity, 'svc-a');
  assert.equal(response.attempts, 2);

  // The fake transport confirmed one byte before challenging.
  assert.equal(transport.calls[0].bytesSent, 1);
  // The content source was actually reopened for the second attempt...
  assert.equal(body.openCount, 2);
  // ...and both attempts sent the full original body.
  assert.equal(transport.calls[0].bodyText, 'payload');
  assert.equal(transport.calls[1].bodyText, 'payload');
  assert.equal(transport.calls[1].bytesSent, 1);

  // Observability: challenge -> reopen authorized -> credential selected.
  const types = eventTypes(client);
  assert.ok(types.includes('challenge'));
  assert.ok(types.includes('body-reopen-authorized'));
  assert.ok(types.includes('credential-selected'));
  assert.equal(lastEvent(client, 'body-reopen-authorized').idempotencyKey, KEY);
  // First attempt had no identity; second one did.
  assert.equal(transport.calls[0].headers.authorization, undefined);
  assert.equal(transport.calls[1].headers.authorization, 'Bearer token-for-svc-a');
});

test('REJECT: one byte confirmed then challenge; one-shot body stops and demands reconfirm without replaying', async () => {
  const transport = createScriptedTransport([
    () => challengeResponse('c-2'),
    () => okResponse(), // must never be reached
  ]);
  const client = new AuthChallengeClient({ transport, credentialProvider: registry() });
  const body = createOneShotSource('cannot-replay');

  await assert.rejects(
    client.send({ method: 'POST',
      url: URL,
      body,
    }),
    (err) => {
      assert.ok(err instanceof ReconfirmRequiredError);
      assert.equal(err.reconfirmRequired, true);
      assert.equal(err.details.reason, 'body-not-reopenable');
      assert.equal(err.details.bytesSent, 1);
      return true;
    },
  );

  // Exactly one wire attempt: the source was not reopened and nothing was resent.
  assert.equal(transport.calls.length, 1);
  assert.equal(body.openCount, 1);
  assert.equal(transport.calls[0].bytesSent, 1);
  const event = lastEvent(client, 'reconfirm-required');
  assert.equal(event.reason, 'body-not-reopenable');
  assert.equal(event.reopenable, false);
});

test('REJECT: reopenable body but missing server idempotency acknowledgment stops and demands reconfirm', async () => {
  const transport = createScriptedTransport([
    () => challengeResponse('c-3'), // no x-idempotency-acknowledged header
  ]);
  const client = new AuthChallengeClient({ transport, credentialProvider: registry() });
  const body = createReopenableSource('replayable');
  body.idempotencyKey = KEY;

  await assert.rejects(
    client.send({ method: 'POST', url: URL, body }),
    (err) => {
      assert.ok(err instanceof ReconfirmRequiredError);
      assert.equal(err.details.reason, 'idempotency-key-not-acknowledged');
      assert.equal(err.details.acknowledged, false);
      return true;
    },
  );
  assert.equal(transport.calls.length, 1);
  assert.equal(body.openCount, 1, 'body must not be reopened without both safety conditions');
});

test('BOUNDARY/INTERLEAVING: acknowledged key value differs from the one sent — the resend is refused', async () => {
  const transport = createScriptedTransport([
    () => challengeResponse('c-4', { extraHeaders: { 'x-idempotency-acknowledged': 'some-other-key' } }),
  ]);
  const client = new AuthChallengeClient({ transport, credentialProvider: registry() });
  const body = createReopenableSource('replayable');
  body.idempotencyKey = KEY;

  await assert.rejects(
    client.send({ method: 'POST', url: URL, body }),
    (err) => err instanceof ReconfirmRequiredError && err.details.reason === 'idempotency-key-not-acknowledged',
  );
  assert.equal(transport.calls.length, 1);
});

test('BOUNDARY: zero bytes sent (empty body) — attaching credentials and retrying needs no idempotency key', async () => {
  const transport = createScriptedTransport([
    () => challengeResponse('c-5'),
    () => okResponse({ ok: true }),
  ]);
  const client = new AuthChallengeClient({ transport, credentialProvider: registry() });

  const response = await client.send({ method: 'POST', url: URL, body: createEmptySource() });

  assert.equal(response.status, 200);
  assert.equal(transport.calls.length, 2);
  assert.equal(transport.calls[0].bytesSent, 0);
  assert.equal(transport.calls[1].headers.authorization, 'Bearer token-for-svc-a');
  // No reconfirm event is possible on the zero-byte path.
  assert.equal(lastEvent(client, 'reconfirm-required'), undefined);
});

test('BOUNDARY: zero-byte one-shot body is still retried because no byte ever left the client', async () => {
  const transport = createScriptedTransport([
    () => challengeResponse('c-6'),
    () => okResponse(),
  ]);
  const client = new AuthChallengeClient({ transport, credentialProvider: registry() });
  const body = createOneShotSource([]);

  const response = await client.send({ method: 'POST', url: URL, body });
  assert.equal(response.status, 200);
  assert.equal(transport.calls.length, 2);
  assert.equal(transport.calls[0].bytesSent, 0);
});

test('INTERLEAVING: challenge after zero bytes is retried, then a second challenge with one byte requires reconfirm', async () => {
  // First attempt carries one byte; challenge c-7 with no ack must stop the
  // chain. A separate zero-byte request sharing nothing else proves the
  // per-request isolation of the negotiated-id set in the negotiation suite.
  const transport = createScriptedTransport([
    () => challengeResponse('c-7'),
  ]);
  const client = new AuthChallengeClient({ transport, credentialProvider: registry() });
  const body = createReopenableSource('second-round');
  body.idempotencyKey = KEY;

  await assert.rejects(
    client.send({ method: 'POST', url: URL, body }),
    (err) => err instanceof ReconfirmRequiredError,
  );

  assert.equal(transport.calls.length, 1);
  assert.equal(transport.calls[0].bytesSent, 1);
  const reconfirms = client.events.filter((e) => e.type === 'reconfirm-required');
  assert.equal(reconfirms.length, 1);
  assert.equal(reconfirms[0].reason, 'idempotency-key-not-acknowledged');
});
