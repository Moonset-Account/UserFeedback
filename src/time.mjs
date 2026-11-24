/**
 * 时间基础工具。
 *
 * 本模块只处理时间戳，不读取系统时钟（除 clock.mjs 中显式提供的可注入时钟外），
 * 以便测试使用完全确定的合成数据。
 *
 * 所有区间统一采用半开区间 [startAt, endAt)：
 *  - startAt 时刻访问有效；
 *  - endAt 时刻视为已离任/已失效。
 */

/**
 * 将外部传入的时间（数字毫秒、ISO 字符串或 Date）规范化为毫秒数。
 * @param {number|string|Date} value
 * @param {string} label 字段名，用于错误信息
 * @returns {number}
 */
export function toTime(value, label = 'time') {
  if (value instanceof Date) {
    const t = value.getTime();
    if (Number.isNaN(t)) {
      throw new TypeError(`${label} 不是有效的 Date`);
    }
    return t;
  }
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) {
      throw new TypeError(`${label} 不是有效的时间戳`);
    }
    return value;
  }
  if (typeof value === 'string') {
    const t = Date.parse(value);
    if (Number.isNaN(t)) {
      throw new TypeError(`${label} 不是有效的 ISO 时间: ${value}`);
    }
    return t;
  }
  throw new TypeError(`${label} 必须是毫秒时间戳、ISO 字符串或 Date`);
}

/**
 * 判断时间点 t 是否落在半开区间 [startAt, endAt) 内。
 * @param {number} t
 * @param {number} startAt
 * @param {number} endAt
 */
export function isWithin(t, startAt, endAt) {
  return t >= startAt && t < endAt;
}

/**
 * 求两个半开区间的交集（仍为半开区间）。无交集时返回 null。
 * @returns {{startAt:number, endAt:number}|null}
 */
export function intersectIntervals(aStart, aEnd, bStart, bEnd) {
  const startAt = Math.max(aStart, bStart);
  const endAt = Math.min(aEnd, bEnd);
  return startAt < endAt ? { startAt, endAt } : null;
}

/**
 * 对区间按开始时间、再按结束时间排序（返回新数组）。
 */
export function sortIntervals(intervals) {
  return [...intervals].sort((a, b) => a.startAt - b.startAt || a.endAt - b.endAt);
}
