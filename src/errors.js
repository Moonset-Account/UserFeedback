/**
 * 登记模块的错误码与类型化错误。
 *
 * 约定：
 * - 违反调用契约（未知 ID、入参形状错误、在已终结的登记上冻结等）抛 RegistrationError；
 * - 业务规则拦截（冻结、会话替换、凭据冲突等）不抛错，而是返回 status=rejected 的终结结果，
 *   并把原因写入 outcome.reason，便于重复回调时原样回放。
 */
export const ErrorCodes = Object.freeze({
  INVALID_ARGUMENT: 'invalid_argument',
  UNKNOWN_REGISTRATION: 'unknown_registration',
  UNKNOWN_ACCOUNT_SESSION: 'unknown_account_session',
  SESSION_REPLACED: 'session_replaced',
  REGISTRATION_FROZEN: 'registration_frozen',
  REGISTRATION_ALREADY_FINISHED: 'registration_already_finished',
  CHALLENGE_ALREADY_USED: 'challenge_already_used',
  NOT_TRUSTED_CREDENTIAL: 'not_trusted_credential',
  CREDENTIAL_ALREADY_BOUND: 'credential_already_bound',
  CREDENTIAL_BOUND_TO_OTHER_ACCOUNT: 'credential_bound_to_other_account',
});

export class RegistrationError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'RegistrationError';
    this.code = code;
    this.details = details;
  }
}
