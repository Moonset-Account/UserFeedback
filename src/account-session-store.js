import { randomUUID } from 'node:crypto';
import { ErrorCodes, RegistrationError } from './errors.js';

/**
 * 账户会话存储：
 * - 一个账户同时只持有一个“当前”会话；
 * - 新会话替换旧会话，旧会话永久标记 replaced，无法再用于完成登记；
 * - 会话对象冻结后对外只暴露快照，外部无法通过引用篡改内部状态。
 */
export class AccountSessionStore {
  #sessions = new Map();

  createSession(accountId, { now = new Date().toISOString() } = {}) {
    if (!accountId || typeof accountId !== 'string') {
      throw new RegistrationError(ErrorCodes.INVALID_ARGUMENT, 'accountId must be a non-empty string');
    }
    const session = Object.freeze({
      sessionId: randomUUID(),
      accountId,
      status: 'active',
      createdAt: now,
      replacedBy: null,
      replacedAt: null,
    });
    this.#sessions.set(session.sessionId, session);
    return this.snapshot(session);
  }

  /**
   * 用新会话替换当前会话。若该账户已有 active 会话，旧会话被标记为 replaced。
   */
  replaceSession(accountId, { now = new Date().toISOString() } = {}) {
    const newSession = this.createSession(accountId, { now });
    for (const existing of this.#sessions.values()) {
      if (existing.accountId !== accountId || existing.sessionId === newSession.sessionId) continue;
      if (existing.status === 'active') {
        this.#sessions.set(
          existing.sessionId,
          Object.freeze({
            ...existing,
            status: 'replaced',
            replacedBy: newSession.sessionId,
            replacedAt: now,
          }),
        );
      }
    }
    return newSession;
  }

  getSession(sessionId) {
    const session = this.#sessions.get(sessionId);
    return session ? this.snapshot(session) : null;
  }

  requireLiveSession(sessionId) {
    const session = this.#sessions.get(sessionId);
    if (!session) {
      throw new RegistrationError(
        ErrorCodes.UNKNOWN_ACCOUNT_SESSION,
        `unknown account session: ${sessionId}`,
      );
    }
    if (session.status !== 'active') {
      throw new RegistrationError(
        ErrorCodes.SESSION_REPLACED,
        `account session ${sessionId} is ${session.status}`,
        { status: session.status, replacedBy: session.replacedBy },
      );
    }
    return this.snapshot(session);
  }

  snapshot(session) {
    return { ...session, ...(session.replacedAt ? {} : {}) };
  }
}
