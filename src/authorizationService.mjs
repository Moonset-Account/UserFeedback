/**
 * 访问凭证核验服务。
 *
 * 领域约束（对应题面）：
 *  1. 工号跟着人走，返聘重新分配任职编号；每个应用的授权绑定到「一次任职」，
 *     授权记录与凭证都带 appointmentId，旧任职补来的材料始终归原任职。
 *  2. 人事记录可能隔天补录（recordedAt 晚于有效区间），也可能更正录错的离任日期
 *     （同一 appointmentId 的更高 revision 覆盖低修订）。
 *  3. 既要判断当下是否可用（verifyNow），也要复核指定时点（verifyAt），
 *     复核支持登记时间视角 asOf，还原「当时系统已知的信息」。
 *  4. 同一人员两次任职时间冲突的时段，访问一律转人工核实，不自动放行也不直接拒绝。
 */
import { toTime, isWithin } from './time.mjs';
import {
  AppointmentNotFoundError,
  AppointmentNotYetRecordedError,
} from './hrStore.mjs';

/** 核验结论 */
export const Decision = Object.freeze({
  ALLOW: 'ALLOW',
  DENY: 'DENY',
  MANUAL_REVIEW: 'MANUAL_REVIEW',
});

/** 拒绝/转人工的稳定原因码，便于调用方与监控按码处理 */
export const Reason = Object.freeze({
  APPOINTMENT_NOT_FOUND: 'APPOINTMENT_NOT_FOUND',
  APPOINTMENT_NOT_YET_RECORDED: 'APPOINTMENT_NOT_YET_RECORDED',
  APPOINTMENT_NOT_ACTIVE: 'APPOINTMENT_NOT_ACTIVE',
  APPOINTMENT_OVERLAP: 'APPOINTMENT_OVERLAP',
  CREDENTIAL_NOT_FOUND: 'CREDENTIAL_NOT_FOUND',
  CREDENTIAL_NOT_YET_ISSUED: 'CREDENTIAL_NOT_YET_ISSUED',
  CREDENTIAL_PERSON_MISMATCH: 'CREDENTIAL_PERSON_MISMATCH',
  CREDENTIAL_APP_MISMATCH: 'CREDENTIAL_APP_MISMATCH',
  AUTHORIZATION_NOT_FOUND: 'AUTHORIZATION_NOT_FOUND',
  AUTHORIZATION_NOT_YET_RECORDED: 'AUTHORIZATION_NOT_YET_RECORDED',
  CREDENTIAL_EXPIRED: 'CREDENTIAL_EXPIRED',
});

class AuthNotFoundError extends Error {
  constructor(message, code) {
    super(message);
    this.name = 'AuthNotFoundError';
    this.code = code;
  }
}

export class AuthorizationService {
  #hrStore;
  #clock;
  /** @type {Map<string, object>} 授权，键为 `${appointmentId}:${appId}` */
  #authorizations = new Map();
  /** @type {Map<string, object>} 凭证，键为 credentialId */
  #credentials = new Map();

  /**
   * @param {import('./hrStore.mjs').HrStore} hrStore
   * @param {import('./clock.mjs').Clock} [clock] 可注入固定时钟
   */
  constructor(hrStore, clock) {
    if (!hrStore) {
      throw new TypeError('hrStore 为必填项');
    }
    this.#hrStore = hrStore;
    this.#clock = clock;
  }

  /**
   * 登记（或补录）一条应用授权。授权绑定一次任职。
   * @param {object} auth
   * @param {string} auth.appointmentId 被绑定的任职编号
   * @param {string} auth.personId      工号
   * @param {string} auth.appId         应用编号
   * @param {number|string|Date} auth.recordedAt 授权登记进入系统的时间（补录可能晚于访问）
   */
  registerAuthorization(auth) {
    if (!auth || typeof auth !== 'object') {
      throw new TypeError('授权必须是对象');
    }
    const { appointmentId, personId, appId, recordedAt } = auth;
    for (const [k, v] of Object.entries({ appointmentId, personId, appId })) {
      if (!v || typeof v !== 'string') {
        throw new TypeError(`${k} 必须是非空字符串`);
      }
    }
    const entry = {
      appointmentId,
      personId,
      appId,
      recordedAt: toTime(recordedAt, 'recordedAt'),
    };
    this.#authorizations.set(this.#authKey(appointmentId, appId), entry);
    return entry;
  }

  /**
   * 登记一张访问凭证。凭证内容为：人员、任职、应用、截止时间。
   * @param {object} cred
   * @param {string} cred.credentialId
   * @param {string} cred.personId       凭证声明的工号
   * @param {string} cred.appointmentId  凭证声明归属的任职编号
   * @param {string} cred.appId          可访问的应用
   * @param {number|string|Date} cred.expiresAt 截止时间（半开：该时刻起失效）
   * @param {number|string|Date} cred.issuedAt  签发时间
   * @param {number|string|Date} [cred.recordedAt] 凭证登记时间，默认取 issuedAt
   */
  registerCredential(cred) {
    if (!cred || typeof cred !== 'object') {
      throw new TypeError('凭证必须是对象');
    }
    const { credentialId, personId, appointmentId, appId, expiresAt, issuedAt } = cred;
    for (const [k, v] of Object.entries({ credentialId, personId, appointmentId, appId })) {
      if (!v || typeof v !== 'string') {
        throw new TypeError(`${k} 必须是非空字符串`);
      }
    }
    const issued = toTime(issuedAt, 'issuedAt');
    const expires = toTime(expiresAt, 'expiresAt');
    if (expires <= issued) {
      throw new RangeError('expiresAt 必须晚于 issuedAt');
    }
    const entry = {
      credentialId,
      personId,
      appointmentId,
      appId,
      issuedAt: issued,
      expiresAt: expires,
      recordedAt: cred.recordedAt === undefined ? issued : toTime(cred.recordedAt, 'recordedAt'),
    };
    this.#credentials.set(credentialId, entry);
    return entry;
  }

  /**
   * 判断当下（时钟当前时刻）一次访问是否可用。默认使用全部已登记信息，
   * 也可传入 asOf 固定登记时间视角，便于在确定性时钟下测试。
   */
  verifyNow(credential, appId, options = {}) {
    const now = this.#resolveNow();
    return this.verifyAt(credential, appId, now, options);
  }

  /**
   * 复核指定时点的一次访问。
   *
   * @param {string|object} credential 凭证对象或 credentialId
   * @param {string} appId 本次访问的目标应用
   * @param {number|string|Date} at 访问发生时刻（有效时间视角）
   * @param {object} [options]
   * @param {number|string|Date} [options.asOf] 登记时间视角上界（含），
   *        省略则使用系统当前全部已知记录（按最新修订复核）
   * @returns {{decision:string, reason:string|null, at:number, asOf:number|null,
   *           credentialId:string|null, appointmentId:string|null, personId:string|null,
   *           appId:string, conflicts:Array}}
   */
  verifyAt(credential, appId, at, options = {}) {
    if (!appId || typeof appId !== 'string') {
      throw new TypeError('appId 必须是非空字符串');
    }
    const accessAt = toTime(at, 'at');
    const asOf = options.asOf === undefined ? null : toTime(options.asOf, 'asOf');
    const cred = typeof credential === 'string'
      ? (this.#credentials.get(credential) ?? null)
      : credential;

    const base = {
      at: accessAt,
      asOf,
      appId,
      credentialId: cred?.credentialId ?? (typeof credential === 'string' ? credential : null),
      appointmentId: cred?.appointmentId ?? null,
      personId: cred?.personId ?? null,
    };
    const deny = (reason, extra = {}) => ({
      decision: Decision.DENY, reason, conflicts: [], ...base, ...extra,
    });
    const review = (reason, conflicts) => ({
      decision: Decision.MANUAL_REVIEW, reason, conflicts, ...base,
    });
    const allow = (extra = {}) => ({
      decision: Decision.ALLOW, reason: null, conflicts: [], ...base, ...extra,
    });

    // 1) 凭证本身在登记时间视角下是否存在 / 是否已签发
    if (!cred) {
      return deny(Reason.CREDENTIAL_NOT_FOUND);
    }
    if (asOf !== null && cred.recordedAt > asOf) {
      return deny(Reason.CREDENTIAL_NOT_YET_ISSUED);
    }

    // 2) 任职记录是否存在、在该视角下是否已登记
    let appointment;
    try {
      appointment = this.#hrStore.getAppointment(cred.appointmentId, asOf ?? undefined);
    } catch (err) {
      if (err instanceof AppointmentNotFoundError) {
        return deny(Reason.APPOINTMENT_NOT_FOUND);
      }
      if (err instanceof AppointmentNotYetRecordedError) {
        return deny(Reason.APPOINTMENT_NOT_YET_RECORDED);
      }
      throw err;
    }

    // 3) 同一人员的两次任职在访问时刻重叠 -> 人工核实（优先于一切自动放行）
    const conflicts = this.#hrStore.findOverlapsAt(appointment.personId, accessAt, asOf ?? undefined);
    if (conflicts.length > 0) {
      return review(Reason.APPOINTMENT_OVERLAP, conflicts);
    }

    // 4) 访问时刻是否落在该任职的有效区间内
    if (!isWithin(accessAt, appointment.startAt, appointment.endAt ?? Number.POSITIVE_INFINITY)) {
      return deny(Reason.APPOINTMENT_NOT_ACTIVE);
    }

    // 5) 凭证与人事任职、目标应用的归属一致性
    if (cred.personId !== appointment.personId) {
      return deny(Reason.CREDENTIAL_PERSON_MISMATCH);
    }
    if (cred.appId !== appId) {
      return deny(Reason.CREDENTIAL_APP_MISMATCH);
    }

    // 6) 该应用对这次任职是否存在授权（授权绑定任职，返聘的新任职需另行授权）
    try {
      this.#getAuthorization(cred.appointmentId, appId, asOf);
    } catch (err) {
      if (err instanceof AuthNotFoundError) {
        return deny(err.code);
      }
      throw err;
    }

    // 7) 凭证截止时间（半开：expiresAt 时刻即失效）
    if (accessAt >= cred.expiresAt) {
      return deny(Reason.CREDENTIAL_EXPIRED);
    }

    return allow();
  }

  /** 取凭证对象（管理端/测试辅助）。 */
  getCredential(credentialId) {
    return this.#credentials.get(credentialId) ?? null;
  }

  #getAuthorization(appointmentId, appId, asOf) {
    const auth = this.#authorizations.get(this.#authKey(appointmentId, appId));
    if (!auth) {
      throw new AuthNotFoundError(
        `任职 ${appointmentId} 对应用 ${appId} 无授权`,
        Reason.AUTHORIZATION_NOT_FOUND,
      );
    }
    if (asOf !== null && auth.recordedAt > asOf) {
      throw new AuthNotFoundError(
        `任职 ${appointmentId} 对应用 ${appId} 的授权截至该视角尚未登记`,
        Reason.AUTHORIZATION_NOT_YET_RECORDED,
      );
    }
    return auth;
  }

  #authKey(appointmentId, appId) {
    return `${appointmentId}:${appId}`;
  }

  #resolveNow() {
    if (this.#clock) {
      return this.#clock.now();
    }
    return Date.now();
  }
}
