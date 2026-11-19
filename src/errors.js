/**
 * 领域错误：所有被控制器拒绝的事件都以 PaperControllerError 抛出，
 * 携带机器可读的 code，便于上层（终端 UI / 审计日志）区分拒绝原因。
 */
export class PaperControllerError extends Error {
  /**
   * @param {string} code 机器可读错误码
   * @param {string} message 人类可读说明
   * @param {object} [context] 附加上下文（纸号、事件 id 等）
   */
  constructor(code, message, context = {}) {
    super(message);
    this.name = 'PaperControllerError';
    this.code = code;
    this.context = context;
  }
}

/** 拒绝原因错误码集中定义，避免魔法字符串散落各处。 */
export const ErrorCode = Object.freeze({
  UNKNOWN_EVENT_TYPE: 'UNKNOWN_EVENT_TYPE',
  MALFORMED_EVENT: 'MALFORMED_EVENT',
  UNKNOWN_PAPER: 'UNKNOWN_PAPER',
  PAPER_ALREADY_REGISTERED: 'PAPER_ALREADY_REGISTERED',
  INVALID_SIDE_POLICY: 'INVALID_SIDE_POLICY',
  SIDE_MISMATCH: 'SIDE_MISMATCH',
  WRONG_PAPER_OWNER: 'WRONG_PAPER_OWNER',
  UNKNOWN_SENSOR: 'UNKNOWN_SENSOR',
  PAPER_QUARANTINED: 'PAPER_QUARANTINED',
  PAPER_RECYCLED: 'PAPER_RECYCLED',
  PAPER_DELIVERED: 'PAPER_DELIVERED',
  WRONG_OWNER: 'WRONG_OWNER',
  GATE_FAILED: 'GATE_FAILED',
});
