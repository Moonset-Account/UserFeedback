import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  lib,
  createCluster,
  registerUser,
  RFC_SALT,
  FIXED_CLIENT_NONCE,
  FIXED_SERVER_NONCE,
} from './helpers.js';

const {
  ScramClient,
  MECHANISMS,
  CODES,
  ScramError,
  primitives,
} = lib;

const RFC_USERNAME = 'user';
const RFC_PASSWORD = 'pencil';
// RFC 7677 §3 固定两端随机量：client nonce + server nonce。
const RFC_FULL_NONCE = 'rOpr3J3HqGQwqkqT7B5eQm8vYb2nFQfv';

// RFC 7677 §3 公布的已知中间量（密码学锚点，独立于本实现）。
const RFC_CLIENT_KEY_B64 = 'gCYQ98ppGGTAxEf1LBxX8v3r1NefmfADrwkLKrv5KZA=';
const RFC_STORED_KEY_B64 = 'rJfX6fYCPN4LAlP+BdXNsrP1+/P/uvfTBh3n6N9WmIc=';
const RFC_SERVER_KEY_B64 = 'wMp60nyqmYuuBBe603Thu+RXAp+QiHI/2WP4kmxunpg=';

// 由上述 RFC 锚点与 RFC 原文 AuthMessage 确定性推出的最终量，
// 作为本套件锁定的已知答案（任何原语回归都会导致不匹配）。
const KAT_PROOF_B64 = 'Gn31nx3SV58TdgsNnhA5KGN0exbvrslTfFMcoGFIzwE=';
const KAT_VERIFIER_B64 = 'ikuPfleBMU5Jy0qttBsTZkArTzARNqEddDw4oD1Frns=';

// RFC 7677 §3 的服务端随机量（与夹具默认值不同，单独注入）。
const RFC_SERVER_NONCE = '7B5eQm8vYb2nFQfv';

function buildRfcCluster() {
  const { stores, worker1 } = createCluster({
    nonceGenerator: () => RFC_SERVER_NONCE,
  });
  registerUser(stores, {
    username: RFC_USERNAME,
    password: RFC_PASSWORD,
    salt: RFC_SALT,
    iterations: 4096,
  });
  return { stores, worker: worker1 };
}

test('RFC 7677 向量：派生中间量与规范公布值一致，证明/服务端签名可复现', async () => {
  const { stores, worker } = buildRfcCluster();

  // 第一层锚点：PBKDF2/HMAC/SHA-256 派生结果必须命中 RFC 公布值。
  const salted = primitives.pbkdf2(RFC_PASSWORD, RFC_SALT, 4096);
  const clientKey = primitives.hmac(salted, 'Client Key');
  const storedKey = primitives.sha256(clientKey);
  const serverKey = primitives.hmac(salted, 'Server Key');
  assert.equal(primitives.toBase64(clientKey), RFC_CLIENT_KEY_B64);
  assert.equal(primitives.toBase64(storedKey), RFC_STORED_KEY_B64);
  assert.equal(primitives.toBase64(serverKey), RFC_SERVER_KEY_B64);

  const client = new ScramClient({
    username: RFC_USERNAME,
    password: RFC_PASSWORD,
    mechanism: MECHANISMS.BASIC,
    clientNonce: FIXED_CLIENT_NONCE,
  });

  const clientFirst = client.firstMessage();
  assert.equal(
    clientFirst.toString('utf8'),
    'n,,n=user,r=rOpr3J3HqGQwqkqT',
    'client-first-message 必须与 RFC 7677 §3 逐字节一致',
  );

  const { serverFirstRaw } = worker.start(clientFirst);
  assert.equal(
    serverFirstRaw.toString('utf8'),
    `r=${RFC_FULL_NONCE},s=${RFC_SALT.toString('base64')},i=4096`,
    'server-first-message 必须与 RFC 7677 §3 逐字节一致',
  );

  const clientFinal = client.finalMessage(serverFirstRaw);
  const finalText = clientFinal.toString('utf8');
  assert.ok(
    finalText.startsWith(`c=biws,r=${RFC_FULL_NONCE},p=`),
    'basic 机制通道绑定字段必须是 base64("n,,") = biws',
  );
  assert.ok(
    finalText.endsWith(`p=${KAT_PROOF_B64}`),
    `客户端证明必须等于锁定答案 ${KAT_PROOF_B64}`,
  );

  const receipt = await worker.final(clientFinal);
  assert.equal(receipt.ok, true);
  assert.equal(receipt.replayed, false);
  assert.equal(
    receipt.serverFinalRaw.toString('utf8'),
    `v=${KAT_VERIFIER_B64}`,
    '服务端签名必须等于锁定答案',
  );

  // 客户端独立校验服务端签名（只信密码学，不信 ok 标志）。
  assert.equal(client.verifyServerFinal(receipt.serverFinalRaw), true);

  // 外部独立重算 HMAC(ServerKey, AuthMessage) 交叉确认。
  const account = stores.accounts.get(RFC_USERNAME);
  assert.equal(primitives.toBase64(account.storedKey), RFC_STORED_KEY_B64);
  const independentVerifier = primitives.hmac(account.serverKey, client.authMessage);
  assert.equal(primitives.toBase64(independentVerifier), KAT_VERIFIER_B64);

  // 恰好创建一个会话。
  assert.equal(stores.sessions.size, 1);
  assert.equal(stores.sessions.get(receipt.sessionId).username, RFC_USERNAME);
});

test('RFC 向量对照：篡改证明字节必须被拒绝，且不得创建会话', async () => {
  const { stores, worker } = buildRfcCluster();
  const client = new ScramClient({
    username: RFC_USERNAME,
    password: RFC_PASSWORD,
    mechanism: MECHANISMS.BASIC,
    clientNonce: FIXED_CLIENT_NONCE,
  });
  const clientFirst = client.firstMessage();
  const { serverFirstRaw } = worker.start(clientFirst);
  const clientFinal = client.finalMessage(serverFirstRaw);

  // 翻转证明段首字符（同长度合法 base64，解码等长但内容不同，模拟伪造证明）。
  const tampered = Buffer.from(clientFinal.toString('utf8').replace(
    KAT_PROOF_B64,
    KAT_PROOF_B64.replace(/^./, (c) => (c === 'G' ? 'H' : 'G')),
  ), 'utf8');

  await assert.rejects(
    () => worker.final(tampered),
    (err) => err instanceof ScramError && err.code === CODES.INVALID_PROOF,
  );
  assert.equal(stores.sessions.size, 0, '证明核验失败时禁止创建登录会话');
});

test('错误密码的证明必须被拒绝', async () => {
  const { stores, worker } = buildRfcCluster();
  const client = new ScramClient({
    username: RFC_USERNAME,
    password: 'wrong-password',
    mechanism: MECHANISMS.BASIC,
    clientNonce: FIXED_CLIENT_NONCE,
  });
  const clientFirst = client.firstMessage();
  const { serverFirstRaw } = worker.start(clientFirst);
  const clientFinal = client.finalMessage(serverFirstRaw);

  await assert.rejects(
    () => worker.final(clientFinal),
    (err) => err instanceof ScramError && err.code === CODES.INVALID_PROOF,
  );
  assert.equal(stores.sessions.size, 0);
});

test('服务端签名被篡改时客户端独立校验必须失败', async () => {
  const { worker } = buildRfcCluster();
  const client = new ScramClient({
    username: RFC_USERNAME,
    password: RFC_PASSWORD,
    mechanism: MECHANISMS.BASIC,
    clientNonce: FIXED_CLIENT_NONCE,
  });
  const clientFirst = client.firstMessage();
  const { serverFirstRaw } = worker.start(clientFirst);
  const clientFinal = client.finalMessage(serverFirstRaw);
  const receipt = await worker.final(clientFinal);

  const tamperedVerifier = Buffer.from(
    receipt.serverFinalRaw.toString('utf8').replace(
      KAT_VERIFIER_B64,
      KAT_VERIFIER_B64.replace(/^./, (c) => (c === 'i' ? 'j' : 'i')),
    ),
    'utf8',
  );
  assert.throws(
    () => client.verifyServerFinal(tamperedVerifier),
    (err) => err instanceof ScramError && err.code === CODES.INVALID_PROOF,
  );
});

void FIXED_SERVER_NONCE;
