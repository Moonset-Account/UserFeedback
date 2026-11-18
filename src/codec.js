// 封存记录与披露副本的 JSON 编解码：Buffer 字段统一以 base64 传输，
// 复核端从 JSON 解析得到的仍是带类型检查的 Buffer，杜绝字符串混淆。
import { FORMAT_VERSION } from './seal.js';
import { DIGEST_BYTES, SALT_BYTES } from './digest.js';

function b64Encode(value) {
  if (!Buffer.isBuffer(value)) throw new TypeError('仅支持 Buffer 字段编码');
  return value.toString('base64');
}

function b64Decode(value, label, expectedLength) {
  if (typeof value !== 'string') throw new TypeError(`${label} 必须是 base64 字符串`);
  const buffer = Buffer.from(value, 'base64');
  if (buffer.length !== expectedLength) {
    throw new RangeError(`${label} 解码后长度必须为 ${expectedLength} 字节，实际 ${buffer.length}`);
  }
  return buffer;
}

export function serializeRecord(record) {
  return JSON.stringify({
    formatVersion: record.formatVersion,
    algorithm: record.algorithm,
    leafCount: record.leafCount,
    root: b64Encode(record.root),
    sealedAt: record.sealedAt,
  });
}

export function parseRecord(jsonText) {
  const raw = typeof jsonText === 'string' ? JSON.parse(jsonText) : jsonText;
  if (!raw || raw.formatVersion !== FORMAT_VERSION) {
    throw new Error('封存记录格式版本不受支持');
  }
  return {
    formatVersion: raw.formatVersion,
    algorithm: raw.algorithm,
    leafCount: raw.leafCount,
    root: b64Decode(raw.root, 'root', DIGEST_BYTES),
    sealedAt: raw.sealedAt,
  };
}

export function serializeBundle(bundle) {
  return JSON.stringify({
    formatVersion: bundle.formatVersion,
    algorithm: bundle.algorithm,
    leafCount: bundle.leafCount,
    entries: bundle.entries.map((entry) => ({
      position: entry.position,
      name: entry.name,
      content: Buffer.isBuffer(entry.content)
        ? { encoding: 'base64', data: entry.content.toString('base64') }
        : entry.content,
      salt: b64Encode(entry.salt),
      digest: b64Encode(entry.digest),
      proof: entry.proof.map((step) => ({
        level: step.level,
        side: step.side,
        digest: b64Encode(step.digest),
      })),
    })),
  });
}

export function parseBundle(jsonText) {
  const raw = typeof jsonText === 'string' ? JSON.parse(jsonText) : jsonText;
  if (!raw || raw.formatVersion !== FORMAT_VERSION) {
    throw new Error('披露副本格式版本不受支持');
  }
  if (!Array.isArray(raw.entries)) throw new Error('披露副本 entries 必须是数组');
  return {
    formatVersion: raw.formatVersion,
    algorithm: raw.algorithm,
    leafCount: raw.leafCount,
    entries: raw.entries.map((entry) => {
      if (!entry || typeof entry !== 'object') throw new Error('副本条目非法');
      if (!Array.isArray(entry.proof)) throw new Error(`位置 ${entry.position} 缺少证明数组`);
      return {
        position: entry.position,
        name: entry.name,
        content: entry.content && typeof entry.content === 'object' && entry.content.encoding === 'base64'
          ? Buffer.from(entry.content.data, 'base64')
          : entry.content,
        salt: b64Decode(entry.salt, `位置 ${entry.position} 的 salt`, SALT_BYTES),
        digest: b64Decode(entry.digest, `位置 ${entry.position} 的 digest`, DIGEST_BYTES),
        proof: entry.proof.map((step) => ({
          level: step.level,
          side: step.side,
          digest: b64Decode(step.digest, 'proof digest', DIGEST_BYTES),
        })),
      };
    }),
  };
}
