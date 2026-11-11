import crypto from 'node:crypto';

/**
 * 纯离线 SHA-256，不依赖任何网络服务。
 */
export function sha256Hex(input) {
  return crypto.createHash('sha256').update(input).digest('hex');
}

/**
 * 按片段顺序重建字段级摘要：fieldDigest = H("field-v1" \n d0 \n d1 ...)。
 * 入参必须是片段摘要数组；两端只要片段摘要序列一致，重建结果就一致，
 * 可用于跨端关联安全校验记录。
 */
export function computeFieldDigest(fragmentDigests) {
  if (!Array.isArray(fragmentDigests)) {
    throw new TypeError('computeFieldDigest 入参必须是片段摘要数组');
  }
  return sha256Hex(['field-v1', ...fragmentDigests].join('\n'));
}

/**
 * 确定性 JSON：对象键排序，保证同一逻辑数据序列化结果字节级一致，
 * 这是报告幂等（reportId 稳定、重复导入结果相同）的基础。
 */
export function stableStringify(value) {
  if (value === null || typeof value !== 'object') {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return '[' + value.map(stableStringify).join(',') + ']';
  }
  const keys = Object.keys(value).sort();
  return '{' + keys.map((k) => JSON.stringify(k) + ':' + stableStringify(value[k])).join(',') + '}';
}

export function canonicalHash(value) {
  return sha256Hex(stableStringify(value));
}
