/**
 * 稳定理由码（stable reason codes）。
 *
 * 校验入口的每个决定都带一个 reason 字段，取值固定为本表中的标识符，
 * 调用方与回归断言可以安全地依赖这些字符串，不会随实现重构而变化。
 */
export const Reason = Object.freeze({
  // 拒绝类
  TOKEN_INVALID: 'TOKEN_INVALID',         // 令牌未通过可信桩核验（伪造/篡改/未知）
  SESSION_MISMATCH: 'SESSION_MISMATCH',   // 令牌绑定的会话与提交会话不一致
  SESSION_REVOKED: 'SESSION_REVOKED',     // 提交会话已被轮换撤销
  ACTION_MISMATCH: 'ACTION_MISMATCH',     // 令牌绑定的动作与提交动作不一致
  TOKEN_EXPIRED: 'TOKEN_EXPIRED',         // 令牌自身有效期已过（独立于宽限期）
  GENERATION_STALE: 'GENERATION_STALE',   // 旧代际且该动作不享宽限，或代际异常
  GRACE_EXPIRED: 'GRACE_EXPIRED',         // 旧代际本享宽限，但宽限窗口已过
  FORM_INVALID: 'FORM_INVALID',           // 结构化字段校验失败（不消耗凭证）
  // 放行类
  ALLOWED: 'ALLOWED',                     // 当前代际凭证，正常放行
  ALLOWED_GRACE: 'ALLOWED_GRACE',         // 上一代凭证在宽限窗口内放行
});
