// 存证摘要（承诺）方案：
// 按固定顺序对 项目 / 报告人 / 发现指纹 / 盐 分别做 UTF-8 编码，
// 每项前置 4 字节大端无符号字节长度，再拼接计算 SHA-256。
import { createHash, timingSafeEqual } from 'node:crypto';

const FIELD_ORDER = Object.freeze(['project', 'reporter', 'finding', 'salt']);

/**
 * 将单个字段按 UTF-8 编码并前置 4 字节大端字节数。
 * @param {string|Buffer} value
 * @returns {Buffer}
 */
function encodeLengthPrefixed(value) {
  const payload = Buffer.isBuffer(value) ? value : Buffer.from(value, 'utf8');
  if (payload.length > 0xffffffff) {
    throw new RangeError('字段 UTF-8 编码后长度超过 4 字节大端可表示范围');
  }
  const header = Buffer.alloc(4);
  header.writeUInt32BE(payload.length, 0);
  return Buffer.concat([header, payload]);
}

/**
 * 计算存证摘要。字段顺序固定为：project, reporter, finding, salt。
 * 入参可为字符串（按 UTF-8 编码）或 Buffer（原样使用，长度按字节计）。
 * @param {{project: string|Buffer, reporter: string|Buffer, finding: string|Buffer, salt: string|Buffer}} preimage
 * @returns {Buffer} 32 字节 SHA-256 摘要
 */
export function computeCommitment(preimage) {
  if (preimage === null || typeof preimage !== 'object') {
    throw new TypeError('preimage 必须是包含 project/reporter/finding/salt 的对象');
  }
  const parts = [];
  for (const key of FIELD_ORDER) {
    const value = preimage[key];
    if (typeof value !== 'string' && !Buffer.isBuffer(value)) {
      throw new TypeError(`字段 ${key} 必须是字符串或 Buffer`);
    }
    parts.push(encodeLengthPrefixed(value));
  }
  return createHash('sha256').update(Buffer.concat(parts)).digest();
}

/**
 * 常量时间比较两个等长摘要，避免揭示校验通过计时侧信道区分。
 * 长度不同或非 32 字节直接返回 false，不抛异常。
 * @param {Buffer} a
 * @param {Buffer} b
 * @returns {boolean}
 */
export function safeEqualDigest(a, b) {
  if (!Buffer.isBuffer(a) || !Buffer.isBuffer(b) || a.length !== 32 || b.length !== 32) {
    return false;
  }
  return timingSafeEqual(a, b);
}

/**
 * 十六进制摘要（便于日志与测试断言）。
 * @param {Buffer} digest
 * @returns {string}
 */
export function toHex(digest) {
  return Buffer.from(digest).toString('hex');
}

/**
 * 从十六进制解析为 32 字节摘要，非法输入抛错。
 * @param {string} hex
 * @returns {Buffer}
 */
export function fromHex(hex) {
  if (typeof hex !== 'string' || !/^[0-9a-fA-F]{64}$/.test(hex)) {
    throw new TypeError('摘要必须是 64 位十六进制字符串');
  }
  return Buffer.from(hex, 'hex');
}

export { FIELD_ORDER };
