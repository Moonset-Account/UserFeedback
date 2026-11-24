/**
 * 接续认证服务（SessionContinuationService）
 *
 * 依据模拟认证设备提供的已校验事件判定共享终端的用屏状态：
 *
 *   - 离场（presence/out）立即锁屏；
 *   - 宽限期内同一成员的认证确认恢复其原会话；
 *   - 超过宽限，或另一位成员完成认证确认，则旧会话结束并另开新会话；
 *   - 进出报告与认证确认允许乱序到达，迟到事件只进历史记录，不改变当前用屏人。
 *
 * 乱序策略（构造参数 ordering）：
 *   - 'sequence'（默认）：按接入编号严格定序。缺口事件先缓冲，待补齐后按序回填；
 *     重复编号的幂等重发被忽略，同编号不同载荷被拒绝。
 *   - 'watermark'：到达即应用，编号不大于已处理水位的事件判定为迟到（stale），
 *     只追加到该成员最近一次会话的审计记录，当前会话继续运行。
 */

import { EVENT_TYPES, eventFingerprint, validateEvent } from './events.js';

export const TERMINAL_STATUS = Object.freeze({
  IDLE: 'idle',
  ACTIVE: 'active',
  LOCKED: 'locked',
});

export const SESSION_STATUS = Object.freeze({
  ACTIVE: 'active',
  LOCKED: 'locked',
  ENDED: 'ended',
});

export const ORDERING = Object.freeze({
  SEQUENCE: 'sequence',
  WATERMARK: 'watermark',
});

export class SessionContinuationService {
  /**
   * @param {object} [options]
   * @param {number} [options.gracePeriodMs=60000] 锁屏宽限期（毫秒），非负数。
   * @param {'sequence'|'watermark'} [options.ordering='sequence'] 乱序处理策略。
   * @param {() => number} [options.clock] 时间源，默认 Date.now。
   */
  constructor({ gracePeriodMs = 60_000, ordering = ORDERING.SEQUENCE, clock = () => Date.now() } = {}) {
    if (typeof gracePeriodMs !== 'number' || !Number.isFinite(gracePeriodMs) || gracePeriodMs < 0) {
      throw new TypeError('gracePeriodMs 必须是非负有限数值（毫秒）');
    }
    if (ordering !== ORDERING.SEQUENCE && ordering !== ORDERING.WATERMARK) {
      throw new TypeError("ordering 必须是 'sequence' 或 'watermark'");
    }
    if (typeof clock !== 'function') {
      throw new TypeError('clock 必须是返回毫秒时间戳的函数');
    }

    this.#gracePeriodMs = gracePeriodMs;
    this.#ordering = ordering;
    this.#clock = clock;
    /** @type {Map<string, object>} */
    this.#terminals = new Map();
  }

  #gracePeriodMs;
  #ordering;
  #clock;
  #terminals;

  get gracePeriodMs() {
    return this.#gracePeriodMs;
  }

  get ordering() {
    return this.#ordering;
  }

  // ---------------------------------------------------------------- 终端内部状态

  #ensureTerminal(terminalId) {
    let terminal = this.#terminals.get(terminalId);
    if (!terminal) {
      terminal = {
        terminalId,
        status: TERMINAL_STATUS.IDLE,
        currentSessionId: null,
        lockedAt: null,
        lastAuthAt: null,
        lastAccessNo: 0,
        /** accessNo -> 已记录事件指纹（应用、重复判定共用） */
        seen: new Map(),
        /** 迟到事件登记表：accessNo -> 指纹集合（不覆盖已应用事件的指纹） */
        staleSeen: new Map(),
        /** sequence 模式下缓冲的缺口事件：accessNo -> { event, fingerprint } */
        pending: new Map(),
        /** 在场成员：memberId -> 最近一次 in 的时间戳 */
        present: new Map(),
        /** sessionId -> 会话记录（含已结束会话） */
        sessions: new Map(),
        /** memberId -> 该成员最近一次会话 id，用于迟到事件归档 */
        sessionByMember: new Map(),
        /** 无法归属到任何会话的事件记录 */
        orphanLog: [],
      };
      this.#terminals.set(terminalId, terminal);
    }
    return terminal;
  }

  // ---------------------------------------------------------------- 公开入口

  /**
   * 处理一条来自模拟认证设备的事件。
   * @param {object} rawEvent
   * @returns {object} 处理结果信封：
   *   { status: 'applied'|'buffered'|'stale'|'duplicate'|'rejected',
   *     terminalId, accessNo, action?, sessionId?, reason?, event, drained? }
   * @throws {TypeError} 事件结构非法。
   */
  handleEvent(rawEvent) {
    const event = validateEvent(rawEvent);
    const terminal = this.#ensureTerminal(event.terminalId);
    const fingerprint = eventFingerprint(event);

    if (this.#ordering === ORDERING.WATERMARK) {
      return this.#routeWatermark(terminal, event, fingerprint);
    }
    return this.#routeSequence(terminal, event, fingerprint);
  }

  // ---------------------------------------------------------------- 乱序路由

  #routeSequence(terminal, event, fingerprint) {
    const { accessNo } = event;

    const buffered = terminal.pending.get(accessNo);
    if (buffered !== undefined) {
      if (buffered.fingerprint === fingerprint) {
        return this.#envelope('duplicate', terminal, event, { action: 'duplicate-ignored' });
      }
      return this.#envelope('rejected', terminal, event, { reason: 'access-no-conflict' });
    }

    if (terminal.seen.has(accessNo)) {
      if (terminal.seen.get(accessNo) === fingerprint) {
        return this.#envelope('duplicate', terminal, event, { action: 'duplicate-ignored' });
      }
      return this.#envelope('rejected', terminal, event, { reason: 'access-no-conflict' });
    }

    const expected = terminal.lastAccessNo + 1;
    if (accessNo > expected) {
      terminal.pending.set(accessNo, { event, fingerprint });
      return this.#envelope('buffered', terminal, event, {
        action: 'buffered',
        waitingFor: expected,
      });
    }

    // accessNo === expected：立即应用，并连续回填缓冲缺口
    const first = this.#apply(terminal, event);
    const drained = [];
    let nextNo = terminal.lastAccessNo + 1;
    while (terminal.pending.has(nextNo)) {
      const next = terminal.pending.get(nextNo);
      terminal.pending.delete(nextNo);
      const result = this.#apply(terminal, next.event);
      drained.push({ accessNo: nextNo, action: result.action, sessionId: result.sessionId });
      nextNo = terminal.lastAccessNo + 1;
    }
    return this.#envelope('applied', terminal, event, {
      action: first.action,
      sessionId: first.sessionId,
      drained,
    });
  }

  #routeWatermark(terminal, event, fingerprint) {
    const { accessNo } = event;

    // 编号已越过当前水位：属于历史窗口。
    // 同载荷是幂等重发；其余（如接手后晚到的旧成员确认、旧进出报告、未补齐的缺口）
    // 一律只归档，不按同号冲突拒绝，也绝不改变当前用屏状态或回退水位。
    if (accessNo <= terminal.lastAccessNo) {
      if (terminal.seen.get(accessNo) === fingerprint) {
        return this.#envelope('duplicate', terminal, event, { action: 'duplicate-ignored' });
      }
      return this.#recordStale(terminal, event);
    }

    // 编号高于水位却已出现过：乱序窗口内的重发/同号冲突
    if (terminal.seen.has(accessNo)) {
      if (terminal.seen.get(accessNo) === fingerprint) {
        return this.#envelope('duplicate', terminal, event, { action: 'duplicate-ignored' });
      }
      return this.#envelope('rejected', terminal, event, { reason: 'access-no-conflict' });
    }

    // 水位被乱序事件顶高后，中间未补齐的缺口编号也只能归档，不能让水位回退
    if (accessNo !== terminal.lastAccessNo + 1) {
      return this.#recordStale(terminal, event);
    }

    const result = this.#apply(terminal, event);
    return this.#envelope('applied', terminal, event, result);
  }

  /** 迟到事件：只入历史审计记录，绝不改变当前用屏状态。 */
  #recordStale(terminal, event) {
    const fingerprint = eventFingerprint(event);
    const bucket = terminal.staleSeen.get(event.accessNo);
    if (bucket) {
      if (bucket.has(fingerprint)) {
        return this.#envelope('duplicate', terminal, event, { action: 'duplicate-ignored' });
      }
      bucket.add(fingerprint);
    } else {
      terminal.staleSeen.set(event.accessNo, new Set([fingerprint]));
    }
    const sessionId = this.#routeSessionId(terminal, event.memberId);
    this.#appendAudit(terminal, event, 'stale-ignored', sessionId);
    return this.#envelope('stale', terminal, event, { action: 'stale-ignored', sessionId });
  }

  #envelope(status, terminal, event, extra = {}) {
    return {
      status,
      terminalId: terminal.terminalId,
      accessNo: event.accessNo,
      event,
      ...extra,
    };
  }

  // ---------------------------------------------------------------- 状态机

  #apply(terminal, event) {
    terminal.lastAccessNo = event.accessNo;
    terminal.seen.set(event.accessNo, eventFingerprint(event));

    if (event.type === EVENT_TYPES.PRESENCE) {
      return this.#applyPresence(terminal, event);
    }
    return this.#applyAuth(terminal, event);
  }

  #applyPresence(terminal, event) {
    const memberId = event.memberId;

    if (event.status === 'in') {
      terminal.present.set(memberId, event.timestamp);
      const sessionId = this.#routeSessionId(terminal, memberId);
      const current = this.#currentSession(terminal);
      const action =
        current && current.memberId === memberId && terminal.status === TERMINAL_STATUS.ACTIVE
          ? 'presence-in-active'
          : 'presence-in-recorded';
      this.#appendAudit(terminal, event, action, sessionId);
      return { action, sessionId };
    }

    // status === 'out'：先维护在场集合；若离场者是当前活跃会话成员则立即锁屏
    terminal.present.delete(memberId);
    const current = this.#currentSession(terminal);
    if (current && current.memberId === memberId && terminal.status === TERMINAL_STATUS.ACTIVE) {
      this.#lock(terminal, current, event.timestamp);
      this.#appendAudit(terminal, event, 'locked-on-leave', current.sessionId);
      return { action: 'locked-on-leave', sessionId: current.sessionId };
    }

    const sessionId = this.#routeSessionId(terminal, memberId);
    this.#appendAudit(terminal, event, 'presence-out-recorded', sessionId);
    return { action: 'presence-out-recorded', sessionId };
  }

  #applyAuth(terminal, event) {
    const memberId = event.memberId;

    if (terminal.status === TERMINAL_STATUS.IDLE) {
      const session = this.#startSession(terminal, event);
      this.#appendAudit(terminal, event, 'session-started', session.sessionId);
      return { action: 'session-started', sessionId: session.sessionId };
    }

    const current = this.#currentSession(terminal);

    if (terminal.status === TERMINAL_STATUS.ACTIVE) {
      if (current.memberId === memberId) {
        terminal.lastAuthAt = event.timestamp;
        this.#appendAudit(terminal, event, 'auth-reaffirmed', current.sessionId);
        return { action: 'auth-reaffirmed', sessionId: current.sessionId };
      }
      this.#endSession(current, event.timestamp, 'taken-over');
      const session = this.#startSession(terminal, event);
      this.#appendAudit(terminal, event, 'takeover-session-started', session.sessionId);
      return { action: 'takeover-session-started', sessionId: session.sessionId };
    }

    // terminal.status === LOCKED
    if (current.memberId === memberId) {
      const withinGrace = event.timestamp - terminal.lockedAt <= this.#gracePeriodMs;
      if (withinGrace) {
        current.status = SESSION_STATUS.ACTIVE;
        current.resumedAt = event.timestamp;
        current.resumeCount += 1;
        terminal.status = TERMINAL_STATUS.ACTIVE;
        terminal.lockedAt = null;
        terminal.lastAuthAt = event.timestamp;
        this.#appendAudit(terminal, event, 'session-resumed', current.sessionId);
        return { action: 'session-resumed', sessionId: current.sessionId };
      }
      this.#endSession(current, event.timestamp, 'grace-expired');
      const session = this.#startSession(terminal, event);
      this.#appendAudit(terminal, event, 'new-session-after-grace', session.sessionId);
      return { action: 'new-session-after-grace', sessionId: session.sessionId };
    }

    // 锁屏期间由另一位成员确认接手
    this.#endSession(current, event.timestamp, 'taken-over');
    const session = this.#startSession(terminal, event);
    this.#appendAudit(terminal, event, 'takeover-session-started', session.sessionId);
    return { action: 'takeover-session-started', sessionId: session.sessionId };
  }

  #startSession(terminal, event) {
    const sessionId = `${terminal.terminalId}#${event.accessNo}`;
    const session = {
      sessionId,
      terminalId: terminal.terminalId,
      memberId: event.memberId,
      status: SESSION_STATUS.ACTIVE,
      startedAt: event.timestamp,
      endedAt: null,
      endReason: null,
      lockedAt: null,
      resumedAt: null,
      resumeCount: 0,
      log: [],
    };
    terminal.sessions.set(sessionId, session);
    terminal.sessionByMember.set(event.memberId, sessionId);
    terminal.currentSessionId = sessionId;
    terminal.status = TERMINAL_STATUS.ACTIVE;
    terminal.lockedAt = null;
    terminal.lastAuthAt = event.timestamp;
    return session;
  }

  #lock(terminal, session, timestamp) {
    terminal.status = TERMINAL_STATUS.LOCKED;
    terminal.lockedAt = timestamp;
    session.status = SESSION_STATUS.LOCKED;
    session.lockedAt = timestamp;
  }

  #endSession(session, timestamp, reason) {
    session.status = SESSION_STATUS.ENDED;
    session.endedAt = timestamp;
    session.endReason = reason;
  }

  #currentSession(terminal) {
    return terminal.currentSessionId ? terminal.sessions.get(terminal.currentSessionId) : null;
  }

  /** 审计归档定位：优先当前会话，其次该成员最近一次会话（可能已结束），否则孤儿记录。 */
  #routeSessionId(terminal, memberId) {
    const current = this.#currentSession(terminal);
    if (current && current.memberId === memberId) return current.sessionId;
    return terminal.sessionByMember.get(memberId) ?? null;
  }

  #appendAudit(terminal, event, action, sessionId) {
    const entry = {
      accessNo: event.accessNo,
      timestamp: event.timestamp,
      action,
      sessionId: sessionId ?? null,
      event,
    };
    if (sessionId && terminal.sessions.has(sessionId)) {
      terminal.sessions.get(sessionId).log.push(entry);
    } else {
      terminal.orphanLog.push(entry);
    }
  }

  // ---------------------------------------------------------------- 时间驱动清理

  /**
   * 扫描所有终端，结束已超过宽限期仍处于锁屏的会话，终端回到 idle。
   * @param {number} [now] 默认为注入时钟的当前时间。
   * @returns {string[]} 本次被结束的会话 id 列表。
   */
  sweepExpired(now = this.#clock()) {
    if (typeof now !== 'number' || !Number.isFinite(now) || now < 0) {
      throw new TypeError('sweepExpired 的 now 必须是非负有限数值');
    }
    const expired = [];
    for (const terminal of this.#terminals.values()) {
      if (
        terminal.status === TERMINAL_STATUS.LOCKED &&
        now - terminal.lockedAt > this.#gracePeriodMs
      ) {
        const session = this.#currentSession(terminal);
        this.#endSession(session, now, 'grace-expired');
        terminal.status = TERMINAL_STATUS.IDLE;
        terminal.currentSessionId = null;
        terminal.lockedAt = null;
        expired.push(session.sessionId);
      }
    }
    return expired;
  }

  // ---------------------------------------------------------------- 查询接口

  /** 终端状态快照（深拷贝，外部修改不影响内部状态）；未知终端返回 null。 */
  getTerminalState(terminalId) {
    const terminal = this.#terminals.get(terminalId);
    if (!terminal) return null;
    return {
      terminalId: terminal.terminalId,
      status: terminal.status,
      currentSessionId: terminal.currentSessionId,
      lockedAt: terminal.lockedAt,
      lastAuthAt: terminal.lastAuthAt,
      lastAccessNo: terminal.lastAccessNo,
      presentMembers: Object.fromEntries(terminal.present),
      pendingAccessNos: [...terminal.pending.keys()].sort((a, b) => a - b),
    };
  }

  /** 当前会话快照（深拷贝）；idle 时返回 null。 */
  getCurrentSession(terminalId) {
    const terminal = this.#terminals.get(terminalId);
    if (!terminal || !terminal.currentSessionId) return null;
    return structuredClone(terminal.sessions.get(terminal.currentSessionId));
  }

  /** 按 id 取会话快照（含已结束会话）；不存在返回 null。 */
  getSession(terminalId, sessionId) {
    const terminal = this.#terminals.get(terminalId);
    if (!terminal) return null;
    const session = terminal.sessions.get(sessionId);
    return session ? structuredClone(session) : null;
  }

  /** 终端上的全部会话快照，按启动接入编号排序。 */
  listSessions(terminalId) {
    const terminal = this.#terminals.get(terminalId);
    if (!terminal) return [];
    return [...terminal.sessions.values()]
      .sort((a, b) => a.startedAt - b.startedAt || a.sessionId.localeCompare(b.sessionId))
      .map((session) => structuredClone(session));
  }

  /**
   * 终端的完整审计轨迹：所有会话记录与孤儿记录合并后按接入编号排序。
   * 迟到事件保留在其归属的旧会话记录中，可在此与当前会话区分核对。
   */
  getAuditTrail(terminalId) {
    const terminal = this.#terminals.get(terminalId);
    if (!terminal) return [];
    const entries = [...terminal.orphanLog];
    for (const session of terminal.sessions.values()) {
      entries.push(...session.log);
    }
    return entries.sort((a, b) => a.accessNo - b.accessNo).map((entry) => structuredClone(entry));
  }

  /** 当前在场成员 id 列表（按最近一次进入的先后顺序）。 */
  getPresentMembers(terminalId) {
    const terminal = this.#terminals.get(terminalId);
    if (!terminal) return [];
    return [...terminal.present.keys()];
  }
}
