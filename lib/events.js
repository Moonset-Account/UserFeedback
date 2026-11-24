/**
 * 模拟认证设备上报的事件定义与校验。
 *
 * 设备被信任为已完成成员身份校验，本模块只做结构与取值范围的防御性校验，
 * 不持有任何凭据，也不执行真实认证。
 *
 * 事件形状（字段均为必填）：
 *   进出报告 { type:'presence', memberId, terminalId, timestamp, accessNo, status:'in'|'out' }
 *   认证确认 { type:'auth',     memberId, terminalId, timestamp, accessNo }
 */

export const EVENT_TYPES = Object.freeze({
  PRESENCE: 'presence',
  AUTH: 'auth',
});

export const PRESENCE_STATUS = Object.freeze({
  IN: 'in',
  OUT: 'out',
});

function isNonEmptyString(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

function isFiniteNonNegativeNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

/**
 * 构造一条进出报告事件。
 * @throws {TypeError} 任一字段非法时抛出，错误信息列出全部非法字段。
 */
export function createPresenceEvent({ memberId, terminalId, timestamp, accessNo, status }) {
  const event = {
    type: EVENT_TYPES.PRESENCE,
    memberId,
    terminalId,
    timestamp,
    accessNo,
    status,
  };
  return validateEvent(event);
}

/**
 * 构造一条认证确认事件。
 * @throws {TypeError} 任一字段非法时抛出。
 */
export function createAuthEvent({ memberId, terminalId, timestamp, accessNo }) {
  const event = {
    type: EVENT_TYPES.AUTH,
    memberId,
    terminalId,
    timestamp,
    accessNo,
  };
  return validateEvent(event);
}

/**
 * 校验事件并返回规范化后的副本（固定键顺序，便于指纹比对）。
 * @param {object} rawEvent
 * @returns {object} 规范化事件副本
 * @throws {TypeError}
 */
export function validateEvent(rawEvent) {
  if (rawEvent === null || typeof rawEvent !== 'object' || Array.isArray(rawEvent)) {
    throw new TypeError('事件必须是一个对象');
  }

  const errors = [];
  const { type, memberId, terminalId, timestamp, accessNo, status } = rawEvent;

  if (!isNonEmptyString(memberId)) errors.push('memberId 必须是非空字符串');
  if (!isNonEmptyString(terminalId)) errors.push('terminalId 必须是非空字符串');
  if (!isFiniteNonNegativeNumber(timestamp)) errors.push('timestamp 必须是非负有限数值（毫秒）');
  if (!Number.isSafeInteger(accessNo) || accessNo < 1) {
    errors.push('accessNo 必须是不小于 1 的安全整数');
  }

  let normalizedType;
  if (type === EVENT_TYPES.PRESENCE) {
    if (status !== PRESENCE_STATUS.IN && status !== PRESENCE_STATUS.OUT) {
      errors.push("presence 事件的 status 必须是 'in' 或 'out'");
    }
    normalizedType = EVENT_TYPES.PRESENCE;
  } else if (type === EVENT_TYPES.AUTH) {
    if (status !== undefined) errors.push('auth 事件不得携带 status 字段');
    normalizedType = EVENT_TYPES.AUTH;
  } else {
    errors.push("type 必须是 'presence' 或 'auth'");
  }

  if (errors.length > 0) {
    throw new TypeError(`非法事件: ${errors.join('；')}`);
  }

  const normalized = {
    type: normalizedType,
    memberId: String(memberId).trim(),
    terminalId: String(terminalId).trim(),
    timestamp,
    accessNo,
  };
  if (normalizedType === EVENT_TYPES.PRESENCE) normalized.status = status;
  return normalized;
}

/**
 * 事件指纹：同一 accessNo 重复上报时用于判断是否为同一条事件的幂等重发。
 */
export function eventFingerprint(event) {
  return JSON.stringify(event);
}
