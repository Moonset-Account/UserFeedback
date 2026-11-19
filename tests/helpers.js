'use strict';

// 测试夹具：固定盐、固定两端随机量、两条可区分的模拟 TLS 1.3 连接。
// 所有随机量都被钉死，使证明/签名可重复、可人工核对。

const path = require('node:path');

const lib = require(path.resolve(__dirname, '..', 'index.js'));

const {
  createSharedStores,
  makeAccountRecord,
  ScramServer,
  TlsConnectionAdapter,
  MECHANISMS,
} = lib;

// 固定盐（RFC 7677 向量使用同一盐：WsZW5dVsFkXUTvUXRHmGapE=）。
const FIXED_SALT = Buffer.from('WesUy1WwNv+2aQe2L4z8kQ==', 'base64');
const FIXED_ITERATIONS = 4096;
const FIXED_CLIENT_NONCE = 'rOpr3J3HqGQwqkqT';
const FIXED_SERVER_NONCE = '7B5eQm8vYb2nTf0p';
const FULL_NONCE = `${FIXED_CLIENT_NONCE}${FIXED_SERVER_NONCE}`;

// RFC 7677 §3 已知向量使用的盐（base64）。
const RFC_SALT = Buffer.from('WsZW5dVsFkXUTvUXRHmGapE=', 'base64');

// 两条模拟连接各自独立的 tls-exporter 导出字节（互不相同、连接内恒定）。
const BINDING_CONN_A = Buffer.from('exporter-binding-connection-A-0001', 'utf8');
const BINDING_CONN_B = Buffer.from('exporter-binding-connection-B-0002', 'utf8');

function fixedServerNonceGenerator() {
  return FIXED_SERVER_NONCE;
}

function createCluster({ nonceGenerator = fixedServerNonceGenerator } = {}) {
  const stores = createSharedStores();
  const worker1 = new ScramServer(stores, {
    workerId: 'worker-1',
    nonceGenerator,
  });
  const worker2 = new ScramServer(stores, {
    workerId: 'worker-2',
    nonceGenerator,
  });
  return { stores, worker1, worker2 };
}

function createConnections() {
  return {
    connA: new TlsConnectionAdapter({
      exportedBinding: BINDING_CONN_A,
      connectionId: 'conn-A',
    }),
    connB: new TlsConnectionAdapter({
      exportedBinding: BINDING_CONN_B,
      connectionId: 'conn-B',
    }),
  };
}

function registerUser(stores, {
  username = 'alice',
  password = 'correct horse battery staple',
  salt = FIXED_SALT,
  iterations = FIXED_ITERATIONS,
  credentialVersion = 1,
} = {}) {
  const record = makeAccountRecord({
    username,
    password,
    salt,
    iterations,
    credentialVersion,
  });
  stores.accounts.put(record);
  return record;
}

/** 跑一遍完整的 PLUS 交换，返回全部中间字节与回执。 */
async function runPlusExchange({
  stores,
  worker,
  connection,
  username = 'alice',
  password = 'correct horse battery staple',
}) {
  const { ScramClient } = lib;
  const client = new ScramClient({
    username,
    password,
    mechanism: MECHANISMS.PLUS,
    connection,
    clientNonce: FIXED_CLIENT_NONCE,
  });
  const clientFirstRaw = client.firstMessage();
  const { serverFirstRaw } = worker.start(clientFirstRaw, connection);
  const clientFinalRaw = client.finalMessage(serverFirstRaw);
  const receipt = await worker.final(clientFinalRaw, connection);
  return { client, clientFirstRaw, serverFirstRaw, clientFinalRaw, receipt };
}

module.exports = {
  lib,
  FIXED_SALT,
  FIXED_ITERATIONS,
  FIXED_CLIENT_NONCE,
  FIXED_SERVER_NONCE,
  FULL_NONCE,
  RFC_SALT,
  BINDING_CONN_A,
  BINDING_CONN_B,
  fixedServerNonceGenerator,
  createCluster,
  createConnections,
  registerUser,
  runPlusExchange,
};
