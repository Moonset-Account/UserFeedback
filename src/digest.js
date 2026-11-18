// 摘要原语：固定域分隔 + 长度前缀编码，避免字段拼接歧义。
// 所有摘要均为 SHA-256，固定 32 字节；盐为每个条目独立生成的 16 字节随机值。
import crypto from 'node:crypto';

export const DIGEST_ALGORITHM = 'sha256';
export const DIGEST_BYTES = 32;
export const SALT_BYTES = 16;

const LEAF_TAG = Buffer.from('evidence-seal/leaf/v1', 'utf8');
const NODE_TAG = Buffer.from('evidence-seal/node/v1', 'utf8');

function toBuffer(value, label) {
  if (Buffer.isBuffer(value)) return value;
  if (typeof value === 'string') return Buffer.from(value, 'utf8');
  throw new TypeError(`${label} 必须是字符串或 Buffer`);
}

function writeLengthPrefixed(buffer) {
  const lengthPrefix = Buffer.alloc(4);
  lengthPrefix.writeUInt32BE(buffer.length, 0);
  return Buffer.concat([lengthPrefix, buffer]);
}

/**
 * 计算叶子承诺：h(LEAF_TAG || len(position) || position || len(salt) || salt
 *                    || len(name) || name || len(content) || content)
 * 位置被纳入承诺：同一内容出现在两个位置时承诺不同，无法互换。
 */
export function leafDigest({ position, salt, name, content }) {
  if (!Number.isInteger(position) || position < 0) {
    throw new TypeError('position 必须是非负整数');
  }
  const saltBuffer = toBuffer(salt, 'salt');
  if (saltBuffer.length !== SALT_BYTES) {
    throw new RangeError(`salt 必须恰好为 ${SALT_BYTES} 字节`);
  }
  const nameBuffer = toBuffer(name, 'name');
  const contentBuffer = toBuffer(content, 'content');
  const positionBuffer = Buffer.from(String(position), 'utf8');

  const hash = crypto.createHash(DIGEST_ALGORITHM);
  hash.update(LEAF_TAG);
  hash.update(writeLengthPrefixed(positionBuffer));
  hash.update(writeLengthPrefixed(saltBuffer));
  hash.update(writeLengthPrefixed(nameBuffer));
  hash.update(writeLengthPrefixed(contentBuffer));
  return hash.digest();
}

/**
 * 计算内部节点承诺：h(NODE_TAG || len(left) || left || len(right) || right)
 * 域分隔保证内部节点摘要不可能被伪造为叶子承诺。
 */
export function nodeDigest(left, right) {
  if (!Buffer.isBuffer(left) || left.length !== DIGEST_BYTES) {
    throw new TypeError('left 必须是 32 字节摘要');
  }
  if (!Buffer.isBuffer(right) || right.length !== DIGEST_BYTES) {
    throw new TypeError('right 必须是 32 字节摘要');
  }
  const hash = crypto.createHash(DIGEST_ALGORITHM);
  hash.update(NODE_TAG);
  hash.update(writeLengthPrefixed(left));
  hash.update(writeLengthPrefixed(right));
  return hash.digest();
}

/** 为一个条目生成独立随机盐（CSPRNG）。 */
export function generateSalt() {
  return crypto.randomBytes(SALT_BYTES);
}

export function digest(value) {
  return crypto.createHash(DIGEST_ALGORITHM).update(toBuffer(value, 'value')).digest();
}
