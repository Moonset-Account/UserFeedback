'use strict';

// SCRAM-SHA-256 / tls-exporter 所需的纯密码学原语封装。
// 全部基于 Node 内置 node:crypto，无第三方依赖。

const crypto = require('node:crypto');

const HASH = 'sha256';
const HASH_LEN = 32;
const DEFAULT_NONCE_BYTES = 24;

/** PBKDF2-HMAC-SHA-256：由密码、盐、迭代数派生 SaltedPassword。 */
function pbkdf2(password, salt, iterations, keylen = HASH_LEN) {
  return crypto.pbkdf2Sync(
    Buffer.from(password, 'utf8'),
    salt,
    iterations,
    keylen,
    HASH,
  );
}

/** HMAC-SHA-256(key, data)。 */
function hmac(key, data) {
  return crypto.createHmac(HASH, key).update(data).digest();
}

/** SHA-256(data)。 */
function sha256(data) {
  return crypto.createHash(HASH).update(data).digest();
}

/** 等长字节串恒定时间异或。 */
function xor(a, b) {
  if (!Buffer.isBuffer(a) || !Buffer.isBuffer(b) || a.length !== b.length) {
    throw new TypeError('xor requires two equal-length Buffers');
  }
  const out = Buffer.allocUnsafe(a.length);
  for (let i = 0; i < a.length; i += 1) {
    out[i] = a[i] ^ b[i];
  }
  return out;
}

function toBase64(buf) {
  return Buffer.from(buf).toString('base64');
}

function fromBase64(text) {
  // Node 支持 base64url；这里显式按标准 base64 解码并做基本合法性检查。
  if (typeof text !== 'string' || text.length === 0) {
    throw new TypeError('base64 input must be a non-empty string');
  }
  return Buffer.from(text, 'base64');
}

/** 恒定时间字节比较，长度不同直接判否。 */
function timingSafeEqual(a, b) {
  if (!Buffer.isBuffer(a) || !Buffer.isBuffer(b)) {
    throw new TypeError('timingSafeEqual requires Buffers');
  }
  if (a.length !== b.length) {
    return false;
  }
  return crypto.timingSafeEqual(a, b);
}

/** 生成不可预测的随机 nonce（base64url 安全字符集）。 */
function randomNonce(bytes = DEFAULT_NONCE_BYTES) {
  return crypto.randomBytes(bytes).toString('base64url');
}

/** 随机会话标识。 */
function randomSessionId() {
  return crypto.randomUUID();
}

module.exports = {
  HASH,
  HASH_LEN,
  pbkdf2,
  hmac,
  sha256,
  xor,
  toBase64,
  fromBase64,
  timingSafeEqual,
  randomNonce,
  randomSessionId,
};
