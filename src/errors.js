/**
 * 通告放行模块统一错误类型。
 * code 为稳定的机器可读错误码，details 附带上下文（段落、收件人等）。
 */
export class AdvisoryError extends Error {
  /**
   * @param {string} code 错误码
   * @param {string} message 人类可读说明
   * @param {Record<string, unknown>} [details] 附加上下文
   */
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'AdvisoryError';
    this.code = code;
    Object.assign(this, details);
  }
}

/**
 * 模块使用的全部错误码：
 * - INVALID_INPUT                 入参格式不合法
 * - PRODUCT_EXISTS                产品重复注册
 * - UNKNOWN_PRODUCT               引用了未注册产品
 * - UNKNOWN_SECTION               引用了不存在的段落
 * - UNKNOWN_ADVISORY              引用了不存在的通告
 * - UNKNOWN_COPY                  引用了不存在的副本
 * - DUPLICATE_SECTION             合并通告中段落重复
 * - SECTION_NOT_IN_ADVISORY       段落不属于该通告
 * - FIX_VERSION_REQUIRED          缺少已确认的修复版本
 * - APPROVAL_REQUIRED             段落改版后未重新绑定批准（或批准版本过期）
 * - SECTION_CONFIRMATION_REQUIRED 合并通告存在未按当前版本确认的段落
 * - PUBLIC_TIME_NOT_REACHED       尚未到达约定公开时间
 * - RECIPIENT_OUT_OF_SCOPE        收件范围越权（包含通告未覆盖的产品）
 * - RECIPIENT_EXISTS              收件人已在通告范围内
 * - RECIPIENT_NOT_FOUND           收件人不在通告范围内，无法移除
 * - NO_ALLOWED_RECIPIENTS         没有任何可接收该段落的有效收件人
 * - RELEASE_BLOCKED               放行评估未通过（reasons 汇总全部阻断原因）
 * - ALREADY_SENT                  通告已发送，内容类写操作被冻结
 * - NOT_SENT                      通告尚未发送，不存在追溯快照
 * - COPY_NOT_QUEUED              副本不在队列中（已出队或不存在）
 * - COPY_VOIDED                  副本已因收件人移除而作废
 */
