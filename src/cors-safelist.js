// CORS 安全清单辅助：判断实际请求中哪些头属于 "CORS-unsafe request-header"，
// 即需要预检许可才能跨源发送的头。参照 Fetch 标准 "CORS-safelisted request-header"。
//
// 说明：本辅助是缓存层的配套工具，只覆盖名称/取值形态判定；
// 标准中按字节长度（128/1024 字节等）的限制在缓存测试中不做强制。

const SAFELISTED_HEADER_NAMES = new Set([
  'accept',
  'accept-language',
  'content-language',
  'content-type',
  'range',
]);

// content-type 仅这些本质类型（essence）属于安全清单。
const SAFELISTED_CONTENT_TYPES = new Set([
  'application/x-www-form-urlencoded',
  'multipart/form-data',
  'text/plain',
]);

// Range 头仅接受 bytes= 起始-结束形式，数字在安全整数范围内。
const SAFE_RANGE_RE = /^bytes=(\d+)?-(\d+)?$/;

function normalizeName(name) {
  if (typeof name !== 'string' || name.trim() === '') {
    throw new TypeError('请求头名称必须是非空字符串');
  }
  return name.trim().toLowerCase();
}

/**
 * 单个头是否在 CORS 安全清单内（不需要预检）。
 * @param {string} name 头名（任意大小写）
 * @param {string} [value=''] 头值
 * @returns {boolean}
 */
export function isCorsSafelistedHeader(name, value = '') {
  const lower = normalizeName(name);
  if (lower === 'content-type') {
    const essence = String(value).split(';')[0].trim().toLowerCase();
    return SAFELISTED_CONTENT_TYPES.has(essence);
  }
  if (lower === 'range') {
    const match = SAFE_RANGE_RE.exec(String(value).trim());
    if (!match) return false;
    const [, start, end] = match;
    if (start !== undefined && !Number.isSafeInteger(Number(start))) return false;
    if (end !== undefined && !Number.isSafeInteger(Number(end))) return false;
    return true;
  }
  return SAFELISTED_HEADER_NAMES.has(lower);
}

/**
 * 从头列表中筛出需要预检许可的头（小写、去重、保序）。
 * @param {Array<{name:string, value?:string}|string>} headers
 * @returns {string[]}
 */
export function selectUnsafeHeaders(headers) {
  if (!Array.isArray(headers)) {
    throw new TypeError('headers 必须是数组');
  }
  const seen = new Set();
  const unsafe = [];
  for (const item of headers) {
    const name = typeof item === 'string' ? item : item?.name;
    const value = typeof item === 'string' ? '' : (item?.value ?? '');
    const lower = normalizeName(name);
    if (seen.has(lower)) continue;
    seen.add(lower);
    if (!isCorsSafelistedHeader(lower, value)) {
      unsafe.push(lower);
    }
  }
  return unsafe;
}

export default { isCorsSafelistedHeader, selectUnsafeHeaders };
