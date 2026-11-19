import { randomBytes } from 'node:crypto';

/** 业务错误：code 为机器可判读的拒绝原因。 */
export class PairingError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'PairingError';
    this.code = code;
  }
}

// 短码字母表：去掉易混淆字符（0/O、1/I/L）。
const CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';

const DEFAULTS = {
  codeLength: 6,          // 屏幕展示的短码长度
  sessionTtlMs: 60_000,   // 短码/会话有效期，到期后短码可再分配
  basePollIntervalMs: 1_000, // 会话初始最小轮询间隔
  maxPollIntervalMs: 30_000, // 退避后的最大轮询间隔
};

/**
 * 会议屏-手机配对会话服务。
 *
 * 交叉约束：
 * 1. 屏幕拿到短码 + 仅供自己轮询的高熵凭据（pollToken，192 bit）。
 * 2. 手机确认页在打开时绑定到当时的会话 id（confirmTicket -> sessionId），
 *    短码到期再分配后，旧确认页的点击只作用于旧会话，不会配给新屏。
 * 3. 轮询过快按会话退避（间隔指数翻倍、封顶），不影响其他会话。
 * 4. 授权结果只能由对应 pollToken 领取一次。
 */
export class PairingService {
  constructor(options = {}) {
    const cfg = { ...DEFAULTS, ...options };
    this.cfg = cfg;
    this.now = options.now ?? (() => Date.now());
    this.random = options.random ?? ((n) => randomBytes(n));

    this.sessions = new Map(); // sessionId -> session
    this.activeCodes = new Map(); // code -> sessionId（仅未过期会话）
    this.tokenIndex = new Map(); // pollToken -> sessionId
    this.tickets = new Map(); // confirmTicket -> { sessionId, used }
  }

  /** 屏幕端：创建配对会话，返回可展示短码与私有轮询凭据。 */
  createScreenSession() {
    const t = this.now();
    const code = this.#allocateCode();
    const pollToken = this.#hex(24); // 192 bit 高熵凭据
    const sessionId = this.#hex(8);
    const session = {
      id: sessionId,
      code,
      pollToken,
      createdAt: t,
      expiresAt: t + this.cfg.sessionTtlMs,
      status: 'pending', // pending -> authorized；到期为 expired
      accountId: null,
      grant: null,
      grantClaimed: false,
      pollIntervalMs: this.cfg.basePollIntervalMs,
      nextPollAt: t, // 首次轮询总是允许
    };
    this.sessions.set(sessionId, session);
    this.activeCodes.set(code, sessionId);
    this.tokenIndex.set(pollToken, sessionId);
    return {
      sessionId,
      code,
      pollToken,
      expiresAt: session.expiresAt,
      pollIntervalMs: session.pollIntervalMs,
    };
  }

  /** 手机端：用短码打开确认页。返回的票据绑定“当时看到的”会话。 */
  openConfirmPage(code) {
    const session = this.#sessionByCode(code);
    if (!session) {
      throw new PairingError('CODE_NOT_FOUND', '短码无效或已过期');
    }
    const confirmTicket = this.#hex(16);
    this.tickets.set(confirmTicket, { sessionId: session.id, used: false });
    return {
      confirmTicket,
      sessionId: session.id,
      code: session.code,
      expiresAt: session.expiresAt,
    };
  }

  /**
   * 手机端：在确认页点击“确认登录”。
   * 票据指向会话 id 而非短码，因此短码被回收再分配后，
   * 旧票据绝不可能作用到持有同一短码的新会话。
   */
  confirmAccount(confirmTicket, accountId) {
    const ticket = this.tickets.get(confirmTicket);
    if (!ticket || ticket.used) {
      throw new PairingError('TICKET_INVALID', '确认票据无效或已使用');
    }
    const session = this.sessions.get(ticket.sessionId);
    if (!session || this.#expireIfDue(session)) {
      throw new PairingError('SESSION_EXPIRED', '该次配对会话已过期');
    }
    if (session.status !== 'pending') {
      throw new PairingError('SESSION_NOT_PENDING', '该次会话已被确认过');
    }
    ticket.used = true;
    session.status = 'authorized';
    session.accountId = accountId;
    session.grant = this.#hex(16); // 一次性授权结果
    return { sessionId: session.id, accountId };
  }

  /**
   * 屏幕端：用私有凭据轮询。
   * 返回 { status }：
   *  - pending     等待手机确认
   *  - throttled   轮询过快，已延长该会话下次可查间隔（retryAfterMs）
   *  - authorized  携带一次性授权结果（仅此一次）
   *  - claimed     授权结果已被领取过
   *  - expired     会话已过期
   */
  poll(pollToken) {
    const sessionId = this.tokenIndex.get(pollToken);
    if (!sessionId) {
      throw new PairingError('POLL_TOKEN_INVALID', '轮询凭据无效');
    }
    const session = this.sessions.get(sessionId);
    if (this.#expireIfDue(session)) {
      return { status: 'expired' };
    }

    const t = this.now();
    if (t < session.nextPollAt) {
      // 该设备轮询过快：仅延长本会话的间隔（指数退避、封顶）。
      session.pollIntervalMs = Math.min(session.pollIntervalMs * 2, this.cfg.maxPollIntervalMs);
      session.nextPollAt = t + session.pollIntervalMs;
      return { status: 'throttled', retryAfterMs: session.nextPollAt - t };
    }
    session.nextPollAt = t + session.pollIntervalMs;

    if (session.status === 'pending') {
      return { status: 'pending' };
    }
    // authorized：授权结果只可由该凭据领取一次。
    if (session.grantClaimed) {
      return { status: 'claimed' };
    }
    session.grantClaimed = true;
    return { status: 'authorized', accountId: session.accountId, grant: session.grant };
  }

  // ---- 内部 ----

  #allocateCode() {
    // 到期短码可再分配：只在“未过期”集合内保证唯一。
    for (let attempt = 0; attempt < 64; attempt++) {
      const bytes = this.random(this.cfg.codeLength);
      let code = '';
      for (let i = 0; i < this.cfg.codeLength; i++) {
        code += CODE_ALPHABET[bytes[i] % CODE_ALPHABET.length];
      }
      const holder = this.activeCodes.get(code);
      if (!holder || this.#expireIfDue(this.sessions.get(holder))) {
        return code;
      }
    }
    throw new PairingError('CODE_EXHAUSTED', '短码空间暂时耗尽');
  }

  #sessionByCode(code) {
    const sessionId = this.activeCodes.get(code);
    if (!sessionId) return null;
    const session = this.sessions.get(sessionId);
    if (!session || this.#expireIfDue(session)) return null;
    return session;
  }

  /** 惰性过期：到期即标记并释放短码。返回是否已过期。 */
  #expireIfDue(session) {
    if (!session) return true;
    if (session.status !== 'expired' && this.now() >= session.expiresAt) {
      session.status = 'expired';
      if (this.activeCodes.get(session.code) === session.id) {
        this.activeCodes.delete(session.code);
      }
    }
    return session.status === 'expired';
  }

  #hex(n) {
    return this.random(n).toString('hex');
  }
}
