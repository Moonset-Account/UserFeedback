'use strict';

// 四类共享存储：账户库、挑战库、幂等回执库、登录会话库。
// 两个工作节点共享同一组存储实例（生产中可替换为 Redis/DB 实现，
// 接口契约保持一致）。所有“至多一次”语义由存储的原子 CAS 保证。

const { ScramError, CODES } = require('./errors');
const { randomSessionId } = require('./primitives');

/**
 * 账户只持久化 SCRAM 所需的非机密材料：
 *   salt / iterations / StoredKey / ServerKey / credentialVersion
 * 永不保存明文密码或 ClientKey。
 */
class AccountStore {
  constructor() {
    this._accounts = new Map();
  }

  put(record) {
    for (const field of ['username', 'salt', 'iterations', 'storedKey', 'serverKey']) {
      if (record[field] === undefined || record[field] === null) {
        throw new TypeError(`account record missing field: ${field}`);
      }
    }
    const stored = {
      username: record.username,
      salt: Buffer.from(record.salt),
      iterations: Number(record.iterations),
      storedKey: Buffer.from(record.storedKey),
      serverKey: Buffer.from(record.serverKey),
      credentialVersion: Number(record.credentialVersion ?? 1),
    };
    this._accounts.set(stored.username, stored);
    return { ...stored };
  }

  get(username) {
    const acct = this._accounts.get(username);
    if (!acct) {
      throw new ScramError(CODES.UNKNOWN_ACCOUNT, `unknown account: ${username}`);
    }
    return acct;
  }

  /**
   * 密码重置：写入新 SCRAM 材料并单调递增凭据版本。
   * 版本提升后，所有在旧版本上签发的挑战在最终提交时立即失效。
   */
  resetPassword(username, next) {
    const current = this._accounts.get(username);
    if (!current) {
      throw new ScramError(CODES.UNKNOWN_ACCOUNT, `unknown account: ${username}`);
    }
    const nextVersion = Math.max(
      current.credentialVersion + 1,
      Number(next.credentialVersion ?? current.credentialVersion + 1),
    );
    const stored = {
      username,
      salt: Buffer.from(next.salt),
      iterations: Number(next.iterations),
      storedKey: Buffer.from(next.storedKey),
      serverKey: Buffer.from(next.serverKey),
      credentialVersion: nextVersion,
    };
    this._accounts.set(username, stored);
    return { ...stored };
  }
}

/** 服务端挑战（阶段一产物），按完整 nonce 索引。 */
class ChallengeStore {
  constructor() {
    this._challenges = new Map();
  }

  put(challenge) {
    this._challenges.set(challenge.nonce, challenge);
  }

  get(nonce) {
    const challenge = this._challenges.get(nonce);
    if (!challenge) {
      throw new ScramError(CODES.UNKNOWN_CHALLENGE, 'challenge not found or expired');
    }
    return challenge;
  }

  /** 标记挑战已完成，阻止同一挑战被二次消费（非重放语义之外的重用）。 */
  complete(nonce) {
    const challenge = this._challenges.get(nonce);
    if (challenge) {
      challenge.completed = true;
    }
  }
}

/**
 * 幂等回执存储。putIfAbsent 是原子的：并发的两个工作节点对同一最终消息
 * 只有一个能插入成功，另一个取回同一回执 —— 因此最多创建一个登录会话。
 */
class IdempotencyStore {
  constructor() {
    this._receipts = new Map();
  }

  /**
   * @returns {{inserted: boolean, receipt: object}}
   */
  putIfAbsent(key, receipt) {
    if (this._receipts.has(key)) {
      return { inserted: false, receipt: this._receipts.get(key) };
    }
    this._receipts.set(key, receipt);
    return { inserted: true, receipt };
  }

  get(key) {
    return this._receipts.has(key) ? this._receipts.get(key) : null;
  }
}

/** 成功登录产生的会话。重复键插入必须失败，由调用方转成幂等返回。 */
class SessionStore {
  constructor() {
    this._sessions = new Map();
  }

  /**
   * 原子插入：仅当 sessionId 不存在时写入。
   * @returns {boolean} 是否为本次新建
   */
  insert(session) {
    if (this._sessions.has(session.sessionId)) {
      return false;
    }
    this._sessions.set(session.sessionId, session);
    return true;
  }

  get(sessionId) {
    return this._sessions.has(sessionId) ? this._sessions.get(sessionId) : null;
  }

  get size() {
    return this._sessions.size;
  }
}

function createSessionId() {
  return randomSessionId();
}

module.exports = {
  AccountStore,
  ChallengeStore,
  IdempotencyStore,
  SessionStore,
  createSessionId,
};
