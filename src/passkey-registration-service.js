import { randomUUID } from 'node:crypto';
import { ErrorCodes, RegistrationError } from './errors.js';
import { TrustedCredential } from './trusted-credential.js';
import { AccountSessionStore } from './account-session-store.js';
import { CredentialStore } from './credential-store.js';
import { RegistrationStatus, RegistrationStore, snapshotRegistration } from './registration-store.js';

/**
 * 通行密钥登记服务。
 *
 * 关键规则（与题面一一对应）：
 * 1. 登记会话绑定“已登录账户的一次性挑战”，挑战全局只能签发一次；
 * 2. 冻结的登记会话、已被替换的账户会话不得完成登记；
 * 3. 同一凭据编号（credentialId）全局首绑生效：同账户重复绑 / 跨账户抢占均被拒绝；
 * 4. 完成回调幂等：同一 registrationId 的重复调用回放首次结果（含拒绝结果），
 *    并额外标注 replay:true；每次回调按该登记自己的状态判断，互不串台；
 * 5. 另一标签页的未完成挑战仍按自己的状态独立判断；
 * 6. 返回当前账户可用凭据列表与每次登记的状态。
 *
 * 完成路径刻意保持同步：所有“检查—写入”在一次事件循环内原子完成，
 * 不跨 await，因此多标签页并发回调时首绑判定不会被交错破坏。
 */
export class PasskeyRegistrationService {
  #sessions;
  #credentials;
  #registrations;
  #verifier;
  #clock;

  constructor({ verifier, sessions, credentials, registrations, clock = () => new Date().toISOString() } = {}) {
    if (!verifier || typeof verifier.verify !== 'function') {
      throw new RegistrationError(ErrorCodes.INVALID_ARGUMENT, 'a verifier with verify() is required');
    }
    this.#verifier = verifier;
    this.#sessions = sessions ?? new AccountSessionStore();
    this.#credentials = credentials ?? new CredentialStore();
    this.#registrations = registrations ?? new RegistrationStore();
    this.#clock = clock;
  }

  // ---- 账户会话（“已登录账户”入口） -------------------------------------

  login(accountId) {
    return this.#sessions.createSession(accountId, { now: this.#clock() });
  }

  /** 重新登录 / 刷新会话：旧会话变为 replaced。 */
  replaceAccountSession(accountId) {
    return this.#sessions.replaceSession(accountId, { now: this.#clock() });
  }

  getAccountSession(sessionId) {
    return this.#sessions.getSession(sessionId);
  }

  // ---- 登记会话 ----------------------------------------------------------

  /**
   * 发起登记：为已登录账户会话签发绑定一次性挑战的登记会话。
   */
  startRegistration({ accountSessionId, challenge }) {
    const session = this.#sessions.requireLiveSession(accountSessionId);
    const registration = this.#registrations.create({
      challenge,
      accountId: session.accountId,
      accountSessionId: session.sessionId,
      now: this.#clock(),
    });
    return registration;
  }

  /** 冻结登记会话（例如用户在另一标签页触发了安全锁定 / 撤销）。 */
  freezeRegistration(registrationId) {
    return this.#registrations.freeze(registrationId, this.#clock());
  }

  /**
   * 验签组件回调，完成登记。
   *
   * @param {object} input
   * @param {string} input.registrationId 登记会话编号（回调的幂等键）
   * @param {TrustedCredential|object} input.credential 验签组件返回的可信凭据结果；
   *        必须是 TrustedCredential 实例，否则按不可信输入拒绝。
   * @returns {{status:'succeeded'|'rejected', replay:boolean, registration:object, credential?:object, reason?:string, details?:object}}
   */
  completeRegistration({ registrationId, credential }) {
    const reg = this.#registrations.require(registrationId);

    // 幂等：任何已终结登记的重复回调，原样回放首次结果。
    if (reg.status === RegistrationStatus.SUCCEEDED || reg.status === RegistrationStatus.REJECTED) {
      return this.#completionResponse(reg, true);
    }

    // 只接受验签组件产出的可信凭据类型，普通对象一律拒绝。
    if (!(credential instanceof TrustedCredential)) {
      return this.#reject(reg, ErrorCodes.NOT_TRUSTED_CREDENTIAL, 'credential is not a TrustedCredential result');
    }

    // 冻结的登记会话不得完成。
    if (reg.status === RegistrationStatus.FROZEN) {
      return this.#reject(
        reg,
        ErrorCodes.REGISTRATION_FROZEN,
        `registration ${registrationId} is frozen`,
        { frozenAt: reg.frozenAt },
      );
    }

    // 已替换的账户会话不得完成（按本登记自己绑定的会话判断）。
    const session = this.#sessions.getSession(reg.accountSessionId);
    if (!session || session.status !== 'active') {
      return this.#reject(
        reg,
        ErrorCodes.SESSION_REPLACED,
        `account session ${reg.accountSessionId} is no longer active`,
        { sessionStatus: session ? session.status : 'missing' },
      );
    }

    // 同一凭据编号只能首次绑定：同账户重复 / 跨账户抢占在此分流。
    const attempt = this.#credentials.tryBind({
      credentialId: credential.credentialId,
      accountId: reg.accountId,
      registrationId: reg.registrationId,
      credential,
      now: this.#clock(),
    });
    if (!attempt.bound) {
      return this.#reject(
        reg,
        attempt.reason === 'credential_already_bound'
          ? ErrorCodes.CREDENTIAL_ALREADY_BOUND
          : ErrorCodes.CREDENTIAL_BOUND_TO_OTHER_ACCOUNT,
        `credential ${credential.credentialId} already bound to account ${attempt.existingAccountId}`,
        { credentialId: credential.credentialId, existingAccountId: attempt.existingAccountId },
      );
    }

    const outcome = {
      ok: true,
      credentialId: credential.credentialId,
      accountId: reg.accountId,
      registrationId: reg.registrationId,
      boundAt: this.#clock(),
    };
    this.#registrations.finish(reg.registrationId, {
      status: RegistrationStatus.SUCCEEDED,
      outcome,
      now: this.#clock(),
    });
    return this.#completionResponse(this.#registrations.require(reg.registrationId), false);
  }

  // ---- 查询 --------------------------------------------------------------

  /** 当前账户可使用的凭据列表（仅成功绑定的凭据）。 */
  listCredentials(accountId) {
    return this.#credentials.listForAccount(accountId);
  }

  /** 账户的每次登记及其状态（含 pending/frozen/rejected/succeeded 全量历史）。 */
  listRegistrations(accountId) {
    return this.#registrations.listForAccount(accountId);
  }

  getRegistration(registrationId) {
    const reg = this.#registrations.get(registrationId);
    if (!reg) return null;
    return snapshotRegistration(reg);
  }

  // ---- 内部 --------------------------------------------------------------

  #reject(reg, reason, message, details = {}) {
    const outcome = {
      ok: false,
      reason,
      message,
      details,
      accountId: reg.accountId,
      registrationId: reg.registrationId,
      rejectedAt: this.#clock(),
    };
    this.#registrations.finish(reg.registrationId, {
      status: RegistrationStatus.REJECTED,
      outcome,
      now: this.#clock(),
    });
    return this.#completionResponse(this.#registrations.require(reg.registrationId), false);
  }

  #completionResponse(reg, replay) {
    const base = {
      status: reg.status,
      replay,
      registration: snapshotRegistration(reg),
    };
    if (reg.status === RegistrationStatus.SUCCEEDED) {
      const binding = this.#credentials.findByCredentialId(reg.outcome.credentialId);
      return { ...base, credential: binding };
    }
    return { ...base, reason: reg.outcome?.reason ?? null, details: reg.outcome?.details ?? {} };
  }

  // 便捷方法：调用验签组件（测试/上层适配可用，服务内部不强制使用）。
  verifyWithVerifier(payload) {
    return this.#verifier.verify(payload);
  }
}

export { randomUUID };
