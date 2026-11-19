import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  lib,
  createCluster,
  createConnections,
  registerUser,
  runPlusExchange,
  FIXED_SALT,
  BINDING_CONN_A,
  BINDING_CONN_B,
  FIXED_CLIENT_NONCE,
} from './helpers.js';

const {
  ScramClient,
  TlsConnectionAdapter,
  MECHANISMS,
  TLS_EXPORTER,
  CODES,
  ScramError,
  primitives,
  makeAccountRecord,
} = lib;

function setup() {
  const cluster = createCluster();
  registerUser(cluster.stores);
  const conns = createConnections();
  return { ...cluster, ...conns };
}

test('PLUS 可接受结果：连接 A 全流程成功，双方证明均按实际交换字节核验通过', async () => {
  const { stores, worker1, connA } = setup();

  const exchange = await runPlusExchange({
    stores,
    worker: worker1,
    connection: connA,
  });

  const { client, clientFirstRaw, serverFirstRaw, clientFinalRaw, receipt } = exchange;

  // 消息形态：GS2 头声明 p=tls-exporter；最终消息 c= 为 gs2 头 + 真实导出字节。
  assert.ok(
    clientFirstRaw.toString('utf8').startsWith(`p=${TLS_EXPORTER},n=alice,`),
    'PLUS 的 client-first 必须携带 p=tls-exporter GS2 头',
  );
  const expectedCbInput = Buffer.concat([
    Buffer.from(`p=${TLS_EXPORTER},`, 'utf8'),
    BINDING_CONN_A,
  ]);
  assert.deepEqual(client.channelBindingInput, expectedCbInput);
  assert.ok(
    clientFinalRaw.toString('utf8').includes(`c=${primitives.toBase64(expectedCbInput)}`),
  );

  assert.equal(receipt.ok, true);
  assert.equal(receipt.mechanism, MECHANISMS.PLUS);
  assert.equal(receipt.connectionId, 'conn-A');

  // 服务端回执中的签名可被客户端独立校验。
  assert.equal(client.verifyServerFinal(receipt.serverFinalRaw), true);

  // 独立重算服务端签名，确认 AuthMessage 确实由实际交换字节构成。
  const account = stores.accounts.get('alice');
  const finalText = clientFinalRaw.toString('utf8');
  const finalWithoutProof = finalText.replace(/,p=[^,]+$/, '');
  // PLUS 的 client-first-bare 从 GS2 头（单字段 p=tls-exporter,）之后开始。
  const firstBare = clientFirstRaw.toString('utf8').replace(/^p=[^,]+,/, '');
  const rebuiltAuthMessage = Buffer.from(
    [
      firstBare,
      serverFirstRaw.toString('utf8'),
      finalWithoutProof,
    ].join(','),
    'utf8',
  );
  assert.deepEqual(rebuiltAuthMessage, client.authMessage);
  const rebuiltVerifier = primitives.hmac(account.serverKey, rebuiltAuthMessage);
  assert.deepEqual(
    primitives.fromBase64(receipt.serverFinalRaw.toString('utf8').slice(2)),
    rebuiltVerifier,
  );

  assert.equal(stores.sessions.size, 1);
});

test('两条连接互不串用：连接 B 的绑定字节无法通过连接 A 挑战的核验', async () => {
  const { stores, worker1, connA, connB } = setup();

  // 阶段一在连接 A 上发起并冻结绑定字节。
  const clientA = new ScramClient({
    username: 'alice',
    password: 'correct horse battery staple',
    mechanism: MECHANISMS.PLUS,
    connection: connA,
    clientNonce: FIXED_CLIENT_NONCE,
  });
  const clientFirst = clientA.firstMessage();
  const { serverFirstRaw } = worker1.start(clientFirst, connA);

  // 客户端用连接 B 的导出字节构造最终消息，并在连接 B 上提交。
  const clientB = new ScramClient({
    username: 'alice',
    password: 'correct horse battery staple',
    mechanism: MECHANISMS.PLUS,
    connection: connB,
    clientNonce: FIXED_CLIENT_NONCE,
  });
  clientB.firstMessage();
  const finalFromB = clientB.finalMessage(serverFirstRaw);

  await assert.rejects(
    () => worker1.final(finalFromB, connB),
    (err) => err instanceof ScramError && err.code === CODES.CONNECTION_MISMATCH,
    '原连接与提交连接不同，必须以 CONNECTION_MISMATCH 拒绝',
  );

  // 即使伪造连接标识（仍走 A 的 id），绑定字节不一致也必须被拦截。
  const spoofedA = new TlsConnectionAdapter({
    exportedBinding: BINDING_CONN_B,
    connectionId: 'conn-A',
  });
  await assert.rejects(
    () => worker1.final(finalFromB, spoofedA),
    (err) => err instanceof ScramError && err.code === CODES.CHANNEL_BINDING_MISMATCH,
    '绑定字节与冻结值不符，必须以 CHANNEL_BINDING_MISMATCH 拒绝',
  );

  assert.equal(stores.sessions.size, 0);
});

test('PLUS 交错边界：同字节跨连接重放走幂等；同挑战的不同消息换连接必须被阻止', async () => {
  const { stores, worker1, worker2, connA, connB } = setup();

  const { clientFinalRaw, receipt } = await runPlusExchange({
    stores,
    worker: worker1,
    connection: connA,
  });
  assert.equal(receipt.ok, true);
  assert.equal(stores.sessions.size, 1);

  // (a) 完全相同的最终消息字节拿到另一条连接、另一个节点上：
  // 幂等判断先于连接判断，必须返回原回执，绝不新建会话。
  const replayed = await worker2.final(clientFinalRaw, connB);
  assert.equal(replayed.replayed, true);
  assert.equal(replayed.sessionId, receipt.sessionId);
  assert.equal(stores.sessions.size, 1);

  // (b) 同一条已完成挑战、不同的最终消息字节、换连接提交：
  // 既不是幂等重放，又落在已消费挑战上，必须拒绝。
  const altered = Buffer.from(
    clientFinalRaw.toString('utf8').replace(/p=(.{4})/, (m, h) =>
      `p=${h === 'AAAA' ? 'BBBB' : 'AAAA'}`),
    'utf8',
  );
  assert.ok(!altered.equals(clientFinalRaw));
  await assert.rejects(
    () => worker2.final(altered, connB),
    (err) => err instanceof ScramError && (
      err.code === CODES.CHALLENGE_COMPLETED
      || err.code === CODES.CONNECTION_MISMATCH
      || err.code === CODES.CHANNEL_BINDING_MISMATCH
    ),
  );
  assert.equal(stores.sessions.size, 1, '被阻止的交错提交不得新建会话');
});

test('PLUS 缺少连接适配器时两端都必须拒绝', () => {
  const { worker1 } = setup();

  // 客户端侧：无连接不能发起 PLUS。
  assert.throws(
    () => new ScramClient({
      username: 'alice',
      password: 'correct horse battery staple',
      mechanism: MECHANISMS.PLUS,
      clientNonce: FIXED_CLIENT_NONCE,
    }),
    (err) => err instanceof ScramError && err.code === CODES.CHANNEL_BINDING_UNSUPPORTED,
  );

  // 服务端侧：收到 PLUS 首消息但没有连接适配器，拒绝签发挑战。
  const client = new ScramClient({
    username: 'alice',
    password: 'correct horse battery staple',
    mechanism: MECHANISMS.PLUS,
    connection: createConnections().connA,
    clientNonce: FIXED_CLIENT_NONCE,
  });
  assert.throws(
    () => worker1.start(client.firstMessage()),
    (err) => err instanceof ScramError && err.code === CODES.CHANNEL_BINDING_REQUIRED,
  );
});

test('同一连接多次导出绑定字节恒定，且两条连接导出值不同', () => {
  const { connA, connB } = createConnections();
  assert.deepEqual(connA.exportChannelBinding(), connA.exportChannelBinding());
  assert.deepEqual(connA.exportChannelBinding(), BINDING_CONN_A);
  assert.deepEqual(connB.exportChannelBinding(), BINDING_CONN_B);
  assert.ok(!BINDING_CONN_A.equals(BINDING_CONN_B));
});

test('账户材料只含盐/迭代数/StoredKey/ServerKey/版本，绝无明文密码或 ClientKey', () => {
  const record = makeAccountRecord({
    username: 'carol',
    password: 'topsecret',
    salt: FIXED_SALT,
    iterations: 4096,
  });
  const keys = Object.keys(record).sort();
  assert.deepEqual(keys, [
    'credentialVersion',
    'iterations',
    'salt',
    'serverKey',
    'storedKey',
    'username',
  ]);
  assert.equal(record.storedKey.length, 32);
  assert.equal(record.serverKey.length, 32);
  const serialized = JSON.stringify(record);
  assert.ok(!serialized.includes('topsecret'));
});
