/**
 * 人事记录存储（双时间）测试：修订覆盖、补录可见性、区间语义、冲突检测。
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { buildHrStore, T } from './fixtures.mjs';
import {
  HrStore,
  AppointmentNotFoundError,
  AppointmentNotYetRecordedError,
} from '../src/index.mjs';

describe('HrStore 修订与登记时间视角', () => {
  test('同一任职高修订号覆盖低修订号（更正离任日期）', () => {
    const hr = buildHrStore();

    // 3月10日之前的视角只能看到修订1：离任日是录错的 02-28
    const before = hr.getAppointment('A-1', T.MAR_10 - 1);
    assert.equal(before.revision, 1);
    assert.equal(before.endAt, T.FEB_28);

    // 最新视角取修订2：离任日更正为 03-31
    const latest = hr.getAppointment('A-1');
    assert.equal(latest.revision, 2);
    assert.equal(latest.endAt, T.MAR_31);

    // 3月10日当天（含）即可见修订2
    const onDay = hr.getAppointment('A-1', T.MAR_10);
    assert.equal(onDay.revision, 2);
  });

  test('隔天补录：补登前查询报 NOT_YET_RECORDED，补登后可见', () => {
    const hr = buildHrStore();

    assert.throws(
      () => hr.getAppointment('B-1', T.FEB_2),
      (err) => err instanceof AppointmentNotYetRecordedError && err.code === 'APPOINTMENT_NOT_YET_RECORDED',
    );

    const appt = hr.getAppointment('B-1', T.FEB_3);
    assert.equal(appt.appointmentId, 'B-1');
    assert.equal(appt.startAt, T.FEB_1);
  });

  test('未知任职编号报 NOT_FOUND', () => {
    const hr = buildHrStore();
    assert.throws(
      () => hr.getAppointment('NOPE'),
      (err) => err instanceof AppointmentNotFoundError && err.code === 'APPOINTMENT_NOT_FOUND',
    );
  });

  test('同一任职编号不允许改变工号，修订号不可重复', () => {
    const hr = new HrStore();
    hr.addAppointmentRecord({
      appointmentId: 'X-1', personId: 'P-1',
      startAt: T.JAN_1, endAt: T.FEB_1, revision: 1, recordedAt: T.JAN_2,
    });
    assert.throws(
      () => hr.addAppointmentRecord({
        appointmentId: 'X-1', personId: 'P-9',
        startAt: T.JAN_1, endAt: T.FEB_1, revision: 2, recordedAt: T.JAN_2,
      }),
      /工号与既有修订不一致/,
    );
    assert.throws(
      () => hr.addAppointmentRecord({
        appointmentId: 'X-1', personId: 'P-1',
        startAt: T.JAN_1, endAt: T.FEB_1, revision: 1, recordedAt: T.JAN_2,
      }),
      /修订号 1 已存在/,
    );
  });

  test('录入校验：endAt 必须晚于 startAt，修订号必须为正整数', () => {
    const hr = new HrStore();
    assert.throws(
      () => hr.addAppointmentRecord({
        appointmentId: 'X-2', personId: 'P-1',
        startAt: T.FEB_1, endAt: T.JAN_1, revision: 1, recordedAt: T.JAN_2,
      }),
      /endAt 必须晚于 startAt/,
    );
    assert.throws(
      () => hr.addAppointmentRecord({
        appointmentId: 'X-3', personId: 'P-1',
        startAt: T.JAN_1, endAt: T.FEB_1, revision: 0, recordedAt: T.JAN_2,
      }),
      /revision 必须是 >= 1 的整数/,
    );
  });
});

describe('HrStore 任职区间与冲突检测', () => {
  test('半开区间：startAt 时刻在任，endAt 时刻已离任；在任任职 endAt=null 持续覆盖', () => {
    const hr = buildHrStore();

    assert.equal(hr.isActiveAt('A-1', T.JAN_1), true);
    assert.equal(hr.isActiveAt('A-1', T.MAR_31), false); // 半开右端点
    assert.equal(hr.isActiveAt('A-2', T.MAY_1), true);
    assert.equal(hr.isActiveAt('A-2', T.FAR_FUTURE), true); // 在任
  });

  test('返聘两次任职互不重叠时无冲突，间隙期无任职覆盖', () => {
    const hr = buildHrStore();

    assert.deepEqual(hr.listAllOverlaps('P-100'), []);
    // 4月20日处于 A-1 结束后、A-2 开始前
    assert.equal(hr.isActiveAt('A-1', T.APR_20), false);
    assert.equal(hr.isActiveAt('A-2', T.APR_20), false);
  });

  test('冲突任职在重叠时刻返回冲突对，且冲突区间为半开交集', () => {
    const hr = buildHrStore();

    // 4月29日：C-1 与 C-2 同时覆盖
    const overlaps = hr.findOverlapsAt('P-300', T.APR_29);
    assert.equal(overlaps.length, 1);
    const ids = [overlaps[0].appointmentA.appointmentId, overlaps[0].appointmentB.appointmentId].sort();
    assert.deepEqual(ids, ['C-1', 'C-2']);
    assert.equal(overlaps[0].overlap.startAt, T.APR_1);
    assert.equal(overlaps[0].overlap.endAt, T.APR_30);

    // 4月30日（C-1 半开右端点）起不再冲突
    assert.deepEqual(hr.findOverlapsAt('P-300', T.APR_30), []);
    // 4月1日（C-2 左端点）起即冲突
    assert.equal(hr.findOverlapsAt('P-300', T.APR_1).length, 1);
    // 3月20日只有 C-1，不冲突
    assert.deepEqual(hr.findOverlapsAt('P-300', T.MAR_20), []);

    const all = hr.listAllOverlaps('P-300');
    assert.equal(all.length, 1);
  });

  test('更正消除冲突：修订把 C-1 离任日提前到 C-2 开始前之后，冲突消失', () => {
    const hr = new HrStore();
    hr.addAppointmentRecord({
      appointmentId: 'C-1', personId: 'P-300',
      startAt: T.JAN_1, endAt: T.APR_30, revision: 1, recordedAt: T.JAN_2,
    });
    hr.addAppointmentRecord({
      appointmentId: 'C-2', personId: 'P-300',
      startAt: T.APR_1, endAt: null, revision: 1, recordedAt: T.APR_1,
    });
    assert.equal(hr.findOverlapsAt('P-300', T.APR_20).length, 1);

    // 修订2：C-1 实际 3月31日离任，4月25日更正
    hr.addAppointmentRecord({
      appointmentId: 'C-1', personId: 'P-300',
      startAt: T.JAN_1, endAt: T.MAR_31, revision: 2, recordedAt: T.APR_25,
    });
    // 最新视角：冲突已消除
    assert.deepEqual(hr.findOverlapsAt('P-300', T.APR_20), []);
    // 更正登记之前的视角：冲突仍然存在，保留历史事实
    assert.equal(hr.findOverlapsAt('P-300', T.APR_20, T.APR_25 - 1).length, 1);
  });

  test('listAppointmentsForPerson 按视角折叠为每任职一条最新修订', () => {
    const hr = buildHrStore();
    const latest = hr.listAppointmentsForPerson('P-100');
    assert.equal(latest.length, 2); // A-1（修订2）与 A-2
    const a1 = latest.find((r) => r.appointmentId === 'A-1');
    assert.equal(a1.revision, 2);

    const oldView = hr.listAppointmentsForPerson('P-100', T.FEB_15);
    const a1Old = oldView.find((r) => r.appointmentId === 'A-1');
    assert.equal(a1Old.revision, 1);
    // 该视角下 A-2 尚未登记
    assert.equal(oldView.some((r) => r.appointmentId === 'A-2'), false);
  });
});
