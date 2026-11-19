'use strict';

// 统一登录端双阶段认证库入口。
// 账户侧只保留盐、迭代数、StoredKey、ServerKey 与凭据版本；
// 明文密码与 ClientKey 只在注册/重置的派生瞬间出现，不落库。

const { pbkdf2, hmac, sha256 } = require('./src/primitives');
const { ScramClient } = require('./src/scram-client');
const { ScramServer } = require('./src/scram-server');
const { TlsConnectionAdapter } = require('./src/connection');
const {
  AccountStore,
  ChallengeStore,
  IdempotencyStore,
  SessionStore,
} = require('./src/storage');
const { ScramError, CODES } = require('./src/errors');
const messages = require('./src/messages');
const primitives = require('./src/primitives');

/**
 * 由明文密码生成可入库的 SCRAM-SHA-256 账户材料（注册/重置时调用一次）。
 * 返回值不含密码与 ClientKey，可直接交给 AccountStore.put / resetPassword。
 */
function makeAccountRecord({
  username,
  password,
  salt,
  iterations = 4096,
  credentialVersion = 1,
}) {
  if (!username || !password || !salt) {
    throw new TypeError('username, password and salt are required');
  }
  const saltBuf = Buffer.isBuffer(salt) ? Buffer.from(salt) : Buffer.from(salt, 'utf8');
  const saltedPassword = pbkdf2(password, saltBuf, iterations);
  const clientKey = hmac(saltedPassword, 'Client Key');
  const storedKey = sha256(clientKey);
  const serverKey = hmac(saltedPassword, 'Server Key');
  return {
    username,
    salt: saltBuf,
    iterations,
    storedKey,
    serverKey,
    credentialVersion,
  };
}

/** 创建一组共享存储（多个工作节点必须传入同一组）。 */
function createSharedStores() {
  return {
    accounts: new AccountStore(),
    challenges: new ChallengeStore(),
    idempotency: new IdempotencyStore(),
    sessions: new SessionStore(),
  };
}

module.exports = {
  // 协议参与者
  ScramClient,
  ScramServer,
  TlsConnectionAdapter,
  // 存储
  createSharedStores,
  AccountStore,
  ChallengeStore,
  IdempotencyStore,
  SessionStore,
  // 注册/重置助手
  makeAccountRecord,
  // 常量与错误
  MECHANISMS: messages.MECHANISMS,
  TLS_EXPORTER: messages.TLS_EXPORTER,
  ScramError,
  CODES,
  primitives,
};
