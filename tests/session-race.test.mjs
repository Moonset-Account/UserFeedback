import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  lib,
  createCluster,
  createConnections,
  registerUser,
  runPlusExchange,
  FIXED_SALT,
} from './helpers.js';

const {
  ScramError,
  CODES,
  makeAccountRecord,
  primitives,
} = lib;

function setup() {
  const cluster = createCluster();
  registerUser(cluster.stores);
  const conns = createConnections();
  return { ...cluster, ...conns };
}

test('同一条最终消息并发交给两个共享存储的工作节点：只创建一个登录会话', async () => {
  const { stores, worker1, worker2, connA } = setup();
  const { ScramClient, MECHANISMS } = lib;

  // 阶段一在 worker1 上完成，最终消息构造好但先不提交。
  const client = new ScramClient({
    username: 'alice',
    password: 'correct horse battery staple',
    mechanism: MECHANISMS.PLUS,
    connection: connA,
    clientNonce: 'concurrent-race-nonce-0001',
  });
  const clientFirst = client.firstMessage();
  const { serverFirstRaw } = worker1.start(clientFirst, connA);
  const clientFinalRaw = client.finalMessage(serverFirstRaw);
  assert.equal(stores.sessions.size, 0, '提交前不应存在会话');

  // 模拟最终消息同时投递到两个节点（并发、同一共享存储、同一原连接）。
  const [r1, r2] = await Promise.all([
    worker1.final(clientFinalRaw, connA),
    worker2.final(clientFinalRaw, connA),
  ]);

  assert.equal(r1.ok, true);
  assert.equal(r2.ok, true);
  assert.equal(
    r1.sessionId,
    r2.sessionId,
    '两个节点必须返回同一个会话标识',
  );
  assert.equal(
    r1.idempotencyKey,
    r2.idempotencyKey,
    '幂等键由最终消息字节决定，两节点计算结果必须一致',
  );
  const replayFlags = [r1.replayed, r2.replayed].filter(Boolean).length;
  assert.equal(replayFlags, 1, '恰好一个节点走重放路径，另一个为原始创建路径');
  const creators = [r1.handledBy, r2.handledBy];
  assert.ok(creators.includes('worker-1') && creators.includes('worker-2'));
  assert.equal(stores.sessions.size, 1, '无论投递多少次，至多一个登录会话');

  // 客户端可独立校验获胜节点返回的服务端签名。
  assert.equal(client.verifyServerFinal(r1.serverFinalRaw), true);
  assert.deepEqual(r1.serverFinalRaw, r2.serverFinalRaw);
});

test('原连接重送同一消息拿到原回执（含同一 sessionId 与同一服务端签名）', async () => {
  const { stores, worker2, connA } = setup();

  const { clientFinalRaw, receipt } = await runPlusExchange({
    stores,
    worker: worker2,
    connection: connA,
  });

  // 改由另一个节点、在原连接上重送完全相同的字节。
  const resent = await worker2.final(clientFinalRaw, connA);
  assert.equal(resent.replayed, true);
  assert.equal(resent.sessionId, receipt.sessionId);
  assert.deepEqual(
    resent.serverFinalRaw,
    receipt.serverFinalRaw,
    '重送返回的服务端签名必须与原回执逐字节相同',
  );
  assert.equal(resent.conversationId, receipt.conversationId);
  assert.equal(stores.sessions.size, 1);
});

test('密码重置先于最终提交：旧挑战立即失效，最终消息被拒绝且无会话', async () => {
  const { stores, worker1, connA } = setup();
  const { ScramClient, MECHANISMS } = lib;

  // 只走到阶段二消息构造、但先不提交：挑战冻结在 credentialVersion=1。
  const pendingClient = new ScramClient({
    username: 'alice',
    password: 'correct horse battery staple',
    mechanism: MECHANISMS.PLUS,
    connection: connA,
    clientNonce: 'pending-reset-nonce-000001',
  });
  const first = pendingClient.firstMessage();
  const { serverFirstRaw } = worker1.start(first, connA);
  const pendingFinal = pendingClient.finalMessage(serverFirstRaw);

  const beforeVersion = stores.accounts.get('alice').credentialVersion;
  const resetRecord = stores.accounts.resetPassword(
    'alice',
    makeAccountRecord({
      username: 'alice',
      password: 'rotated-password',
      salt: FIXED_SALT,
      iterations: 4096,
    }),
  );
  assert.equal(resetRecord.credentialVersion, beforeVersion + 1);

  // 重置之后旧挑战的最终消息才到达：必须按 STALE_CREDENTIAL_VERSION 拒绝。
  await assert.rejects(
    () => worker1.final(pendingFinal, connA),
    (err) => err instanceof ScramError && err.code === CODES.STALE_CREDENTIAL_VERSION,
  );
  assert.equal(stores.sessions.size, 0, '失效挑战不得创建任何会话');
});

test('边界交错：已成功消息在重置之后重送，仍返回原回执而不是重新认证', async () => {
  const { stores, worker1, connA } = setup();

  const { clientFinalRaw, receipt } = await runPlusExchange({
    stores,
    worker: worker1,
    connection: connA,
  });
  const sessionBefore = stores.sessions.size;

  stores.accounts.resetPassword(
    'alice',
    makeAccountRecord({
      username: 'alice',
      password: 'another-rotation',
      salt: FIXED_SALT,
      iterations: 4096,
    }),
  );

  // 幂等判断先于凭据版本判断：同一字节的重放必须拿回原回执。
  const resent = await worker1.final(clientFinalRaw, connA);
  assert.equal(resent.replayed, true);
  assert.equal(resent.sessionId, receipt.sessionId);
  assert.equal(stores.sessions.size, sessionBefore, '重放不允许新建会话');
});

test('同一挑战上提交不同的最终消息：挑战已消费，必须以 CHALLENGE_COMPLETED 拒绝', async () => {
  const { stores, worker1, connA } = setup();

  const { client, clientFirstRaw, serverFirstRaw, clientFinalRaw, receipt } =
    await runPlusExchange({ stores, worker: worker1, connection: connA });
  assert.equal(receipt.ok, true);

  // 用同一 nonce 但构造一份内容不同的最终消息（证明段翻转），
  // 它不是原字节流，因此不走幂等路径，而撞上已完成的挑战。
  const alteredText = clientFinalRaw.toString('utf8').replace(
    /p=(.{4})/,
    (m, head) => `p=${head === 'AAAA' ? 'BBBB' : 'AAAA'}`,
  );
  const altered = Buffer.from(alteredText, 'utf8');
  assert.ok(!altered.equals(clientFinalRaw));
  void client;
  void clientFirstRaw;
  void serverFirstRaw;

  await assert.rejects(
    () => worker1.final(altered, connA),
    (err) => err instanceof ScramError && err.code === CODES.CHALLENGE_COMPLETED,
  );
  assert.equal(stores.sessions.size, 1);
});

test('未知/过期挑战与未知账户分别被明确拒绝', async () => {
  const { stores, worker1, connA } = setup();

  const bogusFinal = Buffer.from(
    'c=biws,r=does-not-exist-nonce,p=QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=',
    'utf8',
  );
  await assert.rejects(
    () => worker1.final(bogusFinal, connA),
    (err) => err instanceof ScramError && err.code === CODES.UNKNOWN_CHALLENGE,
  );

  const { ScramClient, MECHANISMS } = lib;
  const ghost = new ScramClient({
    username: 'ghost',
    password: 'whatever',
    mechanism: MECHANISMS.BASIC,
    clientNonce: 'fixed-ghost-nonce-0001',
  });
  assert.throws(
    () => worker1.start(ghost.firstMessage()),
    (err) => err instanceof ScramError && err.code === CODES.UNKNOWN_ACCOUNT,
  );
  assert.equal(stores.sessions.size, 0);
});

test('重置后的新密码可在新一轮交换中成功，且旧密码被拒绝', async () => {
  const { stores, worker1, connA } = setup();

  stores.accounts.resetPassword(
    'alice',
    makeAccountRecord({
      username: 'alice',
      password: 'rotated-password',
      salt: FIXED_SALT,
      iterations: 4096,
    }),
  );

  // 新密码成功。
  const ok = await runPlusExchange({
    stores,
    worker: worker1,
    connection: connA,
    password: 'rotated-password',
  });
  assert.equal(ok.receipt.ok, true);
  assert.equal(ok.client.verifyServerFinal(ok.receipt.serverFinalRaw), true);

  // 旧密码在新挑战上证明核验失败。
  const { ScramClient, MECHANISMS } = lib;
  const staleClient = new ScramClient({
    username: 'alice',
    password: 'correct horse battery staple',
    mechanism: MECHANISMS.PLUS,
    connection: connA,
    clientNonce: 'fixed-stale-nonce-00000001',
  });
  const first = staleClient.firstMessage();
  const { serverFirstRaw } = worker1.start(first, connA);
  const finalMsg = staleClient.finalMessage(serverFirstRaw);
  await assert.rejects(
    () => worker1.final(finalMsg, connA),
    (err) => err instanceof ScramError && err.code === CODES.INVALID_PROOF,
  );
  void primitives;
});
