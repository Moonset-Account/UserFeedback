'use strict';

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { AuthChallengeClient } from '../src/auth-client.js';
import { createEmptySource } from '../src/body-source.js';
import {
  ChallengeAlreadyNegotiatedError,
  MalformedChallengeError,
} from '../src/errors.js';
import {
  createScriptedTransport,
  createCredentialRegistry,
  challengeResponse,
  okResponse,
  lastEvent,
  credential,
} from './helpers/fakes.mjs';

const ORIGIN = 'https://api.internal.example';
const URL = `${ORIGIN}/v1/whoami`;

function makeClient(routes) {
  const transport = createScriptedTransport(routes);
  const provider = createCredentialRegistry([credential('svc-a', ORIGIN, 'api')]);
  const client = new AuthChallengeClient({ transport, credentialProvider: provider });
  return { transport, provider, client };
}

test('REJECT: the same challenge id is negotiated only once in a request chain', async () => {
  const { transport, provider, client } = makeClient([
    () => challengeResponse('c-1'),
    () => challengeResponse('c-1'), // server repeats the same challenge id
  ]);

  await assert.rejects(
    client.send({ method: 'POST', url: URL, body: createEmptySource() }),
    (err) => {
      assert.ok(err instanceof ChallengeAlreadyNegotiatedError);
      assert.equal(err.challengeId, 'c-1');
      return true;
    },
  );

  assert.equal(transport.calls.length, 2, 'no third wire attempt must happen');
  assert.equal(provider.lookups.length, 1, 'credential is negotiated once; the repeat is refused');
  const rejected = lastEvent(client, 'challenge-rejected');
  assert.equal(rejected.reason, 'already-negotiated');
  assert.equal(rejected.challengeId, 'c-1');
});

test('ACCEPT: distinct challenge ids are each negotiated once and the chain completes', async () => {
  const { transport, provider, client } = makeClient([
    () => challengeResponse('c-1'),
    () => challengeResponse('c-2'),
    () => okResponse({ ok: true }),
  ]);

  const response = await client.send({ method: 'POST', url: URL, body: createEmptySource() });
  assert.equal(response.status, 200);
  assert.equal(response.attempts, 3);
  assert.equal(provider.lookups.length, 2);
  assert.deepEqual(
    provider.lookups.map((l) => l.realm),
    ['api', 'api'],
  );
  assert.deepEqual(
    client.events.filter((e) => e.type === 'challenge').map((e) => e.challengeId),
    ['c-1', 'c-2'],
  );
});

test('BOUNDARY: negotiation memory is per request — a new send may reuse an old challenge id', async () => {
  const { client } = makeClient([
    () => challengeResponse('c-1'),
    () => okResponse(),
    () => challengeResponse('c-1'),
    () => okResponse(),
  ]);

  const first = await client.send({ method: 'POST', url: URL, body: createEmptySource() });
  assert.equal(first.status, 200);

  // Same client instance, fresh request chain: c-1 must be negotiable again.
  const second = await client.send({ method: 'POST', url: URL, body: createEmptySource() });
  assert.equal(second.status, 200);
  assert.equal(second.attempts, 2);
});

test('REJECT: a 401 without challenge id is a malformed challenge and never loops', async () => {
  const transport = createScriptedTransport([
    () => ({ status: 401, headers: { 'www-authenticate': 'Bearer realm="api"' } }),
  ]);
  const client = new AuthChallengeClient({
    transport,
    credentialProvider: createCredentialRegistry([credential('svc-a', ORIGIN, 'api')]),
  });

  await assert.rejects(
    client.send({ method: 'GET', url: URL }),
    (err) => err instanceof MalformedChallengeError,
  );
  assert.equal(transport.calls.length, 1);
});
