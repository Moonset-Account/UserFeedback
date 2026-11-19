'use strict';

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { parseChallenge } from '../src/challenge.js';
import {
  createReopenableSource,
  createOneShotSource,
  createEmptySource,
} from '../src/body-source.js';
import { MalformedChallengeError } from '../src/errors.js';

test('parseChallenge reads scheme, realm and stable challenge id', () => {
  const challenge = parseChallenge('Bearer realm="api", challenge_id="c-1", error="invalid_token"');
  assert.equal(challenge.scheme, 'bearer');
  assert.equal(challenge.realm, 'api');
  assert.equal(challenge.challengeId, 'c-1');
  assert.equal(challenge.params.error, 'invalid_token');
});

test('parseChallenge handles quoted strings containing escaped separators', () => {
  const challenge = parseChallenge('Digest realm="a\\"b,c", challenge_id=42');
  assert.equal(challenge.realm, 'a"b,c');
  assert.equal(challenge.challengeId, '42');
});

test('parseChallenge rejects a header without challenge id', () => {
  assert.throws(
    () => parseChallenge('Bearer realm="api"'),
    (err) => err instanceof MalformedChallengeError,
  );
});

test('parseChallenge rejects an empty or missing header', () => {
  assert.throws(() => parseChallenge(''), (err) => err instanceof MalformedChallengeError);
  assert.throws(() => parseChallenge(undefined), (err) => err instanceof MalformedChallengeError);
});

test('reopenable source can be reopened and reports open count', async () => {
  const source = createReopenableSource(['ab', 'cd']);
  assert.equal(source.reopenable, true);
  assert.equal(source.byteLength, 4);

  const first = await drain(source.open());
  assert.equal(first, 'abcd');
  const second = await drain(source.open());
  assert.equal(second, 'abcd');
  assert.equal(source.openCount, 2);
});

test('one-shot source can only be opened once', async () => {
  const source = createOneShotSource('payload');
  assert.equal(source.reopenable, false);
  assert.equal(await drain(source.open()), 'payload');
  assert.equal(source.openCount, 1);
  await assert.rejects(() => drain(source.open()), /can only be opened once/);
});

test('empty source reports zero bytes and stays reopenable', async () => {
  const source = createEmptySource();
  assert.equal(source.byteLength, 0);
  assert.equal(source.reopenable, true);
  assert.equal(await drain(source.open()), '');
  assert.equal(source.openCount, 1);
});

async function drain(iterable) {
  const decoder = new TextDecoder();
  let text = '';
  for await (const chunk of iterable) text += decoder.decode(chunk, { stream: true });
  text += decoder.decode();
  return text;
}
