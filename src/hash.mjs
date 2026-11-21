// 散列槽计算：FNV-1a（32 位）取模 16384，与 Redis Cluster 的槽数量一致。
// 纯函数、确定性、无外部依赖，仅使用 Node 标准库。

export const SLOT_COUNT = 16384;

/**
 * 计算键所属的散列槽。
 * @param {string} key
 * @returns {number} 范围 [0, 16383]
 */
export function keySlot(key) {
  if (typeof key !== 'string' || key.length === 0) {
    throw new TypeError('key 必须是非空字符串');
  }
  // FNV-1a 32 位
  let hash = 0x811c9dc5;
  for (let i = 0; i < key.length; i += 1) {
    hash ^= key.charCodeAt(i);
    // Math.imul 保证 32 位带符号乘法语义
    hash = Math.imul(hash, 0x01000193);
  }
  // 无符号化后取模
  return (hash >>> 0) % SLOT_COUNT;
}

/**
 * 生成一个必定落在指定槽内的键（测试与合成数据用）。
 * @param {number} slot
 * @param {string} [prefix]
 * @returns {string}
 */
export function findKeyForSlot(slot, prefix = 'k') {
  if (!Number.isInteger(slot) || slot < 0 || slot >= SLOT_COUNT) {
    throw new RangeError(`slot 必须是 [0, ${SLOT_COUNT - 1}] 内的整数`);
  }
  let i = 0;
  for (;;) {
    const candidate = `${prefix}:${slot}:${i}`;
    if (keySlot(candidate) === slot) return candidate;
    i += 1;
  }
}

/**
 * 生成 n 个同槽键（用于多键读场景）。
 * @param {number} slot
 * @param {number} count
 * @returns {string[]}
 */
export function keysForSlot(slot, count) {
  const out = [];
  let i = 0;
  while (out.length < count) {
    const candidate = `grp:${slot}:${i}`;
    if (keySlot(candidate) === slot) out.push(candidate);
    i += 1;
  }
  return out;
}
