/**
 * 人事系统任职记录存储（双时间模型）。
 *
 * 每条任职修订记录包含：
 *  - appointmentId 任职编号：返聘会分配新的编号，授权与凭证都按它归属；
 *  - personId      工号：跟着人走，返聘不变；
 *  - startAt/endAt 任职有效区间（半开 [startAt, endAt)，endAt 为 null 表示在任）；
 *  - revision      修订号：同一任职编号下，修订号越大越新，后者更正前者；
 *  - recordedAt    登记时间：该修订进入人事系统的时间（可能隔天补录）。
 *
 * 「复核指定时点」因此有两个正交视角：
 *  - 有效时间视角：访问发生的时刻 at，由任职区间判断；
 *  - 登记时间视角：asOf，只使用 recordedAt <= asOf 的记录，
 *    模拟「在 asOf 那个时刻系统里已知的信息」。
 */
import { toTime, isWithin, intersectIntervals, sortIntervals } from './time.mjs';

export class AppointmentNotFoundError extends Error {
  constructor(appointmentId) {
    super(`任职记录不存在: ${appointmentId}`);
    this.name = 'AppointmentNotFoundError';
    this.code = 'APPOINTMENT_NOT_FOUND';
    this.appointmentId = appointmentId;
  }
}

export class AppointmentNotYetRecordedError extends Error {
  constructor(appointmentId, asOf) {
    super(`截至 ${new Date(asOf).toISOString()} 任职 ${appointmentId} 的记录尚未登记`);
    this.name = 'AppointmentNotYetRecordedError';
    this.code = 'APPOINTMENT_NOT_YET_RECORDED';
    this.appointmentId = appointmentId;
    this.asOf = asOf;
  }
}

export class HrStore {
  /** @type {Map<string, Array<object>>} appointmentId -> 修订记录（登记顺序） */
  #records = new Map();

  /**
   * 录入一条任职修订记录。
   * @param {object} rec
   * @param {string} rec.appointmentId 任职编号
   * @param {string} rec.personId      工号
   * @param {number|string|Date} rec.startAt 任职开始
   * @param {number|string|Date|null} [rec.endAt] 任职结束，null/省略表示在任
   * @param {number} rec.revision      修订号（整数，越大越新）
   * @param {number|string|Date} rec.recordedAt 登记进入系统的时间
   */
  addAppointmentRecord(rec) {
    if (!rec || typeof rec !== 'object') {
      throw new TypeError('任职记录必须是对象');
    }
    const { appointmentId, personId, revision, recordedAt } = rec;
    if (!appointmentId || typeof appointmentId !== 'string') {
      throw new TypeError('appointmentId 必须是非空字符串');
    }
    if (!personId || typeof personId !== 'string') {
      throw new TypeError('personId 必须是非空字符串');
    }
    if (!Number.isInteger(revision) || revision < 1) {
      throw new TypeError('revision 必须是 >= 1 的整数');
    }

    const startAt = toTime(rec.startAt, 'startAt');
    const endAt = rec.endAt === null || rec.endAt === undefined ? null : toTime(rec.endAt, 'endAt');
    if (endAt !== null && endAt <= startAt) {
      throw new RangeError(`任职 ${appointmentId} 的 endAt 必须晚于 startAt`);
    }
    const recAt = toTime(recordedAt, 'recordedAt');

    const list = this.#records.get(appointmentId);
    if (list) {
      // 同一任职编号在语义上是同一个人的同一次任职，工号不允许被修订改变。
      if (list[0].personId !== personId) {
        throw new Error(`任职 ${appointmentId} 的工号与既有修订不一致，拒绝录入`);
      }
      if (list.some((r) => r.revision === revision)) {
        throw new Error(`任职 ${appointmentId} 的修订号 ${revision} 已存在`);
      }
    }

    const entry = { appointmentId, personId, startAt, endAt, revision, recordedAt: recAt };
    this.#records.set(appointmentId, [...(this.#records.get(appointmentId) ?? []), entry]);
    return entry;
  }

  /**
   * 取某任职「截至 asOf 已知的最新修订」。
   * @param {string} appointmentId
   * @param {number} asOf 登记时间上界（含）；undefined 表示使用全部已知记录
   * @returns {object} 规范化后的任职修订
   */
  getAppointment(appointmentId, asOf) {
    const list = this.#records.get(appointmentId);
    if (!list || list.length === 0) {
      throw new AppointmentNotFoundError(appointmentId);
    }
    const visible = asOf === undefined
      ? list
      : list.filter((r) => r.recordedAt <= asOf);
    if (visible.length === 0) {
      throw new AppointmentNotYetRecordedError(appointmentId, asOf);
    }
    return visible.reduce((latest, r) => (r.revision > latest.revision ? r : latest));
  }

  /**
   * 列出某工号下截至 asOf 可见的全部任职（每个任职编号取最新修订）。
   * @returns {Array<object>}
   */
  listAppointmentsForPerson(personId, asOf) {
    const result = [];
    for (const [appointmentId, list] of this.#records) {
      const visible = asOf === undefined ? list : list.filter((r) => r.recordedAt <= asOf);
      if (visible.length === 0) continue;
      const latest = visible.reduce((a, b) => (b.revision > a.revision ? b : a));
      if (latest.personId === personId) {
        result.push(latest);
      }
    }
    return result;
  }

  /**
   * 找出同一人员在时间点 at 处于重叠状态的任职对。
   *
   * 两段不同任职的有效区间在 at 时刻同时覆盖该点，即构成需要人工核实的冲突。
   * 只比较不同的任职编号；同一任职的历史修订已由 getAppointment 折叠为最新值。
   *
   * @param {string} personId
   * @param {number} at 访问时刻（毫秒）
   * @param {number} [asOf] 登记时间视角
   * @returns {Array<{appointmentA:object, appointmentB:object, overlap:{startAt:number,endAt:number}}>}
   */
  findOverlapsAt(personId, at, asOf) {
    const appointments = this.listAppointmentsForPerson(personId, asOf)
      .filter((a) => this.#covers(a, at));
    const conflicts = [];
    for (let i = 0; i < appointments.length; i += 1) {
      for (let j = i + 1; j < appointments.length; j += 1) {
        const a = appointments[i];
        const b = appointments[j];
        const overlap = intersectIntervals(
          a.startAt,
          a.endAt ?? Number.POSITIVE_INFINITY,
          b.startAt,
          b.endAt ?? Number.POSITIVE_INFINITY,
        );
        if (overlap) {
          conflicts.push({ appointmentA: a, appointmentB: b, overlap });
        }
      }
    }
    return conflicts;
  }

  /**
   * 列出同一人员两两任职的全部重叠区间（供管理端/人工核实使用，与具体访问时刻无关）。
   * @returns {Array<{appointmentA:object, appointmentB:object, overlap:object}>}
   */
  listAllOverlaps(personId, asOf) {
    const appointments = sortIntervals(this.listAppointmentsForPerson(personId, asOf));
    const conflicts = [];
    for (let i = 0; i < appointments.length; i += 1) {
      for (let j = i + 1; j < appointments.length; j += 1) {
        const a = appointments[i];
        const b = appointments[j];
        const overlap = intersectIntervals(
          a.startAt,
          a.endAt ?? Number.POSITIVE_INFINITY,
          b.startAt,
          b.endAt ?? Number.POSITIVE_INFINITY,
        );
        if (overlap) {
          conflicts.push({ appointmentA: a, appointmentB: b, overlap });
        }
      }
    }
    return conflicts;
  }

  /**
   * 判断某任职的有效区间（按指定视角的最新修订）是否覆盖时间点 at。
   */
  isActiveAt(appointmentId, at, asOf) {
    const appt = this.getAppointment(appointmentId, asOf);
    return this.#covers(appt, at);
  }

  #covers(appt, at) {
    return isWithin(at, appt.startAt, appt.endAt ?? Number.POSITIVE_INFINITY);
  }
}
