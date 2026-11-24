/**
 * 本地合成人事/授权数据（全部为虚构数据，不读取任何外部系统）。
 *
 * 时间线（毫秒时间戳，均为 2026 年的虚构日期）：
 *
 * 人员 P-100 的第一次任职 A-1：
 *   有效区间 2026-01-01 ~ 2026-03-31（修订1，1月2日登记）
 *   修订2 把离任日从错误的 02-28 更正为 03-31（3月10日才登记）
 *   —— 覆盖「更正曾经录错的离任日期」
 *
 * 人员 P-100 返聘后的第二次任职 A-2：
 *   有效区间 2026-05-01 起在任（4月25日登记）
 *   —— 返聘重新分配任职编号；新任职需另行授权
 *
 * 人员 P-200 的任职 B-1：
 *   有效区间 2026-02-01 ~ 2026-06-30，但记录 2 月 3 日才补登
 *   —— 覆盖「隔天补到」
 *
 * 人员 P-300 的两次互相冲突的任职 C-1 / C-2：
 *   C-1: 2026-01-01 ~ 2026-04-30
 *   C-2: 2026-04-01 ~ 在任
 *   重叠区间 2026-04-01 ~ 2026-04-30 —— 覆盖「时间冲突需人工核实」
 */
import { HrStore } from '../src/hrStore.mjs';
import { AuthorizationService } from '../src/authorizationService.mjs';
import { Clock } from '../src/clock.mjs';

const D = (iso) => Date.parse(iso);

export const T = Object.freeze({
  JAN_1: D('2026-01-01T00:00:00Z'),
  JAN_2: D('2026-01-02T00:00:00Z'),
  FEB_1: D('2026-02-01T00:00:00Z'),
  FEB_2: D('2026-02-02T12:00:00Z'),
  FEB_3: D('2026-02-03T00:00:00Z'),
  FEB_15: D('2026-02-15T12:00:00Z'),
  FEB_28: D('2026-02-28T00:00:00Z'),
  MAR_10: D('2026-03-10T00:00:00Z'),
  MAR_20: D('2026-03-20T12:00:00Z'),
  MAR_31: D('2026-03-31T00:00:00Z'),
  APR_1: D('2026-04-01T00:00:00Z'),
  APR_10: D('2026-04-10T00:00:00Z'),
  APR_20: D('2026-04-20T12:00:00Z'),
  APR_25: D('2026-04-25T00:00:00Z'),
  APR_29: D('2026-04-29T12:00:00Z'),
  APR_30: D('2026-04-30T00:00:00Z'),
  MAY_1: D('2026-05-01T00:00:00Z'),
  MAY_15: D('2026-05-15T12:00:00Z'),
  JUN_15: D('2026-06-15T12:00:00Z'),
  JUN_30: D('2026-06-30T00:00:00Z'),
  JUL_1: D('2026-07-01T00:00:00Z'),
  FAR_FUTURE: D('2099-01-01T00:00:00Z'),
});

export function buildHrStore() {
  const hr = new HrStore();

  // P-100 第一次任职 A-1：修订1 的离任日录错为 02-28
  hr.addAppointmentRecord({
    appointmentId: 'A-1', personId: 'P-100',
    startAt: T.JAN_1, endAt: T.FEB_28, revision: 1, recordedAt: T.JAN_2,
  });
  // 修订2：更正离任日为 03-31，3月10日才登记
  hr.addAppointmentRecord({
    appointmentId: 'A-1', personId: 'P-100',
    startAt: T.JAN_1, endAt: T.MAR_31, revision: 2, recordedAt: T.MAR_10,
  });

  // P-100 返聘：新任职编号 A-2，5月1日起在任
  hr.addAppointmentRecord({
    appointmentId: 'A-2', personId: 'P-100',
    startAt: T.MAY_1, endAt: null, revision: 1, recordedAt: T.APR_25,
  });

  // P-200：任职有效区间从 2月1日开始，但记录 2月3日才补登
  hr.addAppointmentRecord({
    appointmentId: 'B-1', personId: 'P-200',
    startAt: T.FEB_1, endAt: T.JUN_30, revision: 1, recordedAt: T.FEB_3,
  });

  // P-300：两次任职区间冲突
  hr.addAppointmentRecord({
    appointmentId: 'C-1', personId: 'P-300',
    startAt: T.JAN_1, endAt: T.APR_30, revision: 1, recordedAt: T.JAN_2,
  });
  hr.addAppointmentRecord({
    appointmentId: 'C-2', personId: 'P-300',
    startAt: T.APR_1, endAt: null, revision: 1, recordedAt: T.APR_1,
  });

  return hr;
}

export function buildAuthorizations(service) {
  // 应用 APP-X 对 A-1 的授权（1月2日登记）
  service.registerAuthorization({
    appointmentId: 'A-1', personId: 'P-100', appId: 'APP-X', recordedAt: T.JAN_2,
  });
  // 应用 APP-X 对返聘新任职 A-2 的授权（5月1日登记）
  service.registerAuthorization({
    appointmentId: 'A-2', personId: 'P-100', appId: 'APP-X', recordedAt: T.MAY_1,
  });
  // 应用 APP-Y 仅授权给 A-2（用于验证授权与任职绑定）
  service.registerAuthorization({
    appointmentId: 'A-2', personId: 'P-100', appId: 'APP-Y', recordedAt: T.MAY_1,
  });
  // P-200 的授权在任职记录补登之后才登记
  service.registerAuthorization({
    appointmentId: 'B-1', personId: 'P-200', appId: 'APP-X', recordedAt: T.FEB_3,
  });
  // P-300 冲突任职各自的授权
  service.registerAuthorization({
    appointmentId: 'C-1', personId: 'P-300', appId: 'APP-X', recordedAt: T.JAN_2,
  });
  service.registerAuthorization({
    appointmentId: 'C-2', personId: 'P-300', appId: 'APP-X', recordedAt: T.APR_1,
  });
}

export function buildCredentials(service) {
  // P-100 第一次任职的凭证，3月20日截止
  service.registerCredential({
    credentialId: 'CRED-A1',
    personId: 'P-100', appointmentId: 'A-1', appId: 'APP-X',
    issuedAt: T.JAN_2, expiresAt: T.MAR_20, recordedAt: T.JAN_2,
  });
  // P-100 返聘后新任职的新凭证
  service.registerCredential({
    credentialId: 'CRED-A2',
    personId: 'P-100', appointmentId: 'A-2', appId: 'APP-X',
    issuedAt: T.MAY_1, expiresAt: T.FAR_FUTURE, recordedAt: T.MAY_1,
  });
  // P-100 针对 APP-Y 的新任职凭证
  service.registerCredential({
    credentialId: 'CRED-A2-Y',
    personId: 'P-100', appointmentId: 'A-2', appId: 'APP-Y',
    issuedAt: T.MAY_1, expiresAt: T.FAR_FUTURE, recordedAt: T.MAY_1,
  });
  // P-200：凭证 2月2日签发，但任职记录 2月3日才补登（隔天补到）
  service.registerCredential({
    credentialId: 'CRED-B1',
    personId: 'P-200', appointmentId: 'B-1', appId: 'APP-X',
    issuedAt: T.FEB_2, expiresAt: T.JUN_30, recordedAt: T.FEB_2,
  });
  // P-300 冲突任职的凭证
  service.registerCredential({
    credentialId: 'CRED-C1',
    personId: 'P-300', appointmentId: 'C-1', appId: 'APP-X',
    issuedAt: T.JAN_2, expiresAt: T.FAR_FUTURE, recordedAt: T.JAN_2,
  });
  service.registerCredential({
    credentialId: 'CRED-C2',
    personId: 'P-300', appointmentId: 'C-2', appId: 'APP-X',
    issuedAt: T.APR_1, expiresAt: T.FAR_FUTURE, recordedAt: T.APR_1,
  });
}

export function buildService(fixedNow) {
  const hr = buildHrStore();
  const service = new AuthorizationService(hr, fixedNow === undefined ? undefined : new Clock(fixedNow));
  buildAuthorizations(service);
  buildCredentials(service);
  return { hr, service };
}
