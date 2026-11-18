import { randomUUID } from 'node:crypto';
import { ErrorCodes, RegistrationError } from './errors.js';

export const RegistrationStatus = Object.freeze({
  PENDING: 'pending',
  FROZEN: 'frozen',
  SUCCEEDED: 'succeeded',
  REJECTED: 'rejected',
});

export function snapshotRegistration(reg) {
  return {
    ...reg,
    outcome: reg.outcome ? structuredClone(reg.outcome) : null,
  };
}

/**
 * 登记会话存储：
 * - 每个登记会话绑定一个账户会话与一次性挑战；
 * - 挑战值全局唯一，重复发起即拒绝（一次性语义）；
 * - 终结结果（succeeded/rejected）随会话持久化，供重复回调原样回放；
 * - 所有状态转移集中在此处，服务层负责编排业务规则。
 */
export class RegistrationStore {
  #registrations = new Map();
  #challengeIndex = new Map();

  create({ challenge, accountId, accountSessionId, now }) {
    if (!challenge || typeof challenge !== 'string') {
      throw new RegistrationError(ErrorCodes.INVALID_ARGUMENT, 'challenge must be a non-empty string');
    }
    if (this.#challengeIndex.has(challenge)) {
      throw new RegistrationError(
        ErrorCodes.CHALLENGE_ALREADY_USED,
        `one-time challenge already issued: ${challenge}`,
        { challenge, registrationId: this.#challengeIndex.get(challenge) },
      );
    }
    const registrationId = randomUUID();
    const registration = {
      registrationId,
      challenge,
      accountId,
      accountSessionId,
      status: RegistrationStatus.PENDING,
      createdAt: now,
      frozenAt: null,
      finishedAt: null,
      outcome: null,
    };
    this.#registrations.set(registrationId, registration);
    this.#challengeIndex.set(challenge, registrationId);
    return snapshotRegistration(registration);
  }

  get(registrationId) {
    const reg = this.#registrations.get(registrationId);
    return reg ? snapshotRegistration(reg) : null;
  }

  require(registrationId) {
    const reg = this.#registrations.get(registrationId);
    if (!reg) {
      throw new RegistrationError(
        ErrorCodes.UNKNOWN_REGISTRATION,
        `unknown registration session: ${registrationId}`,
      );
    }
    return reg;
  }

  freeze(registrationId, now) {
    const reg = this.require(registrationId);
    if (reg.status === RegistrationStatus.SUCCEEDED || reg.status === RegistrationStatus.REJECTED) {
      throw new RegistrationError(
        ErrorCodes.REGISTRATION_ALREADY_FINISHED,
        `registration ${registrationId} already finished`,
      );
    }
    if (reg.status === RegistrationStatus.FROZEN) {
      return snapshotRegistration(reg);
    }
    reg.status = RegistrationStatus.FROZEN;
    reg.frozenAt = now;
    return snapshotRegistration(reg);
  }

  finish(registrationId, { status, outcome, now }) {
    const reg = this.require(registrationId);
    reg.status = status;
    reg.outcome = structuredClone(outcome);
    reg.finishedAt = now;
    return snapshotRegistration(reg);
  }

  listForAccount(accountId) {
    return [...this.#registrations.values()]
      .filter((r) => r.accountId === accountId)
      .sort((a, b) => a.createdAt.localeCompare(b.createdAt))
      .map(snapshotRegistration);
  }
}
