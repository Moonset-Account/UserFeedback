/**
 * AuthorizationService 核验测试。
 *
 * 覆盖题面要求：
 *  - 正常访问放行；任职区间、凭证截止时间的半开边界；
 *  - 返聘新任职需新凭证/新授权，旧任职材料始终归原任职，不串用；
 *  - 离任日期更正后，按最新记录复核与按历史视角复核结论不同；
 *  - 隔天补录：补登前的视角拒绝（记录未到），事后复核放行；
 *  - 两次任职时间冲突时段一律人工核实；
 *  - 凭证/授权/应用/人员归属不一致的拒绝原因码；
 *  - verifyNow 经固定时钟判定当下可用性。
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { buildService, T } from './fixtures.mjs';
import { Decision, Reason, Clock, AuthorizationService, HrStore } from '../src/index.mjs';

describe('正常访问与边界', () => {
  test('任职在任、授权存在、凭证未过期 -> ALLOW', () => {
    const { service } = buildService();
    const r = service.verifyAt('CRED-A1', 'APP-X', T.FEB_15);
    assert.equal(r.decision, Decision.ALLOW);
    assert.equal(r.reason, null);
    assert.equal(r.appointmentId, 'A-1');
    assert.equal(r.personId, 'P-100');
  });

  test('任职区间半开边界：开始时刻放行，离任时刻拒绝', () => {
    const { service } = buildService();

    assert.equal(service.verifyAt('CRED-A1', 'APP-X', T.JAN_1).decision, Decision.ALLOW);
    const atEnd = service.verifyAt('CRED-A1', 'APP-X', T.MAR_31);
    assert.equal(atEnd.decision, Decision.DENY);
    assert.equal(atEnd.reason, Reason.APPOINTMENT_NOT_ACTIVE);
  });

  test('凭证截止时间半开边界：截止前一刻放行，截止时刻起按过期拒绝', () => {
    const { service } = buildService();

    const justBefore = service.verifyAt('CRED-A1', 'APP-X', T.MAR_20 - 1);
    assert.equal(justBefore.decision, Decision.ALLOW);

    const atExpiry = service.verifyAt('CRED-A1', 'APP-X', T.MAR_20);
    assert.equal(atExpiry.decision, Decision.DENY);
    assert.equal(atExpiry.reason, Reason.CREDENTIAL_EXPIRED);
  });

  test('间隙期与离任后的访问被拒绝', () => {
    const { service } = buildService();

    const gap = service.verifyAt('CRED-A1', 'APP-X', T.APR_20);
    assert.equal(gap.decision, Decision.DENY);
    assert.equal(gap.reason, Reason.APPOINTMENT_NOT_ACTIVE);
  });
});

describe('返聘：工号跟人走，授权与凭证绑定任职编号', () => {
  test('返聘后新任职 + 新凭证 -> ALLOW；旧凭证不作用于新任职时期', () => {
    const { service } = buildService();

    assert.equal(service.verifyAt('CRED-A2', 'APP-X', T.MAY_15).decision, Decision.ALLOW);

    // 旧凭证声明的任职 A-1 在 5 月已结束
    const oldCredOnNewJob = service.verifyAt('CRED-A1', 'APP-X', T.MAY_15);
    assert.equal(oldCredOnNewJob.decision, Decision.DENY);
    assert.equal(oldCredOnNewJob.reason, Reason.APPOINTMENT_NOT_ACTIVE);
  });

  test('旧任职补来的授权/凭证仍归原任职：旧凭证只在原任职区间内可用', () => {
    const { service } = buildService();

    // 即使在返聘后拿旧凭证复核第一次任职期间的访问，归属仍是 A-1，可放行
    const historical = service.verifyAt('CRED-A1', 'APP-X', T.FEB_15);
    assert.equal(historical.decision, Decision.ALLOW);
    assert.equal(historical.appointmentId, 'A-1');

    // 新任职凭证不能回溯作用于第一次任职
    const newCredBackdated = service.verifyAt('CRED-A2', 'APP-X', T.FEB_15);
    assert.equal(newCredBackdated.decision, Decision.DENY);
    assert.equal(newCredBackdated.reason, Reason.APPOINTMENT_NOT_ACTIVE);
  });

  test('授权绑定应用与任职：A-1 对 APP-Y 无授权，A-2 才有', () => {
    const { service } = buildService();

    const yOnNewJob = service.verifyAt('CRED-A2-Y', 'APP-Y', T.MAY_15);
    assert.equal(yOnNewJob.decision, Decision.ALLOW);

    // 用 A-1 时期的凭证访问 APP-Y：既无授权，且任职已结束（先命中任职不活跃）
    const oldJobY = service.verifyAt('CRED-A1', 'APP-Y', T.FEB_15);
    assert.equal(oldJobY.decision, Decision.DENY);
    assert.equal(oldJobY.reason, Reason.CREDENTIAL_APP_MISMATCH);
  });
});

describe('人事修订：更正录错的离任日期', () => {
  test('按更正前视角：3月20日在错误离任日之后 -> 拒绝；最新视角：更正后放行', () => {
    const { service } = buildService();

    // 历史视角（3月10日修订登记之前）：修订1 的离任日是 02-28
    const oldView = service.verifyAt('CRED-A1', 'APP-X', T.MAR_20, { asOf: T.MAR_10 - 1 });
    assert.equal(oldView.decision, Decision.DENY);
    assert.equal(oldView.reason, Reason.APPOINTMENT_NOT_ACTIVE);

    // 最新视角（含修订2）：离任日更正为 03-31，3月20日仍在任；
    // 但凭证 03-20 截止，访问时刻取 3月19日以隔离凭证过期因素
    const latest = service.verifyAt('CRED-A1', 'APP-X', T.MAR_20 - 1);
    assert.equal(latest.decision, Decision.ALLOW);

    // 修订登记当天的视角即可见更正
    const onRevisionDay = service.verifyAt('CRED-A1', 'APP-X', T.MAR_20 - 1, { asOf: T.MAR_10 });
    assert.equal(onRevisionDay.decision, Decision.ALLOW);
  });

  test('更正后 3月20日访问因凭证截止被拒（边界原因隔离正确）', () => {
    const { service } = buildService();
    const r = service.verifyAt('CRED-A1', 'APP-X', T.MAR_20);
    assert.equal(r.decision, Decision.DENY);
    assert.equal(r.reason, Reason.CREDENTIAL_EXPIRED);
  });
});

describe('隔天补录：记录晚于有效时间到达', () => {
  test('补登前的视角：任职记录与凭证都未到 -> 拒绝；补登后复核 -> 放行', () => {
    const { service } = buildService();

    // 2月2日视角：B-1 记录 2月3日才登记，凭证也是 2月2日签发（同日可见），
    // 先命中任职尚未登记
    const before = service.verifyAt('CRED-B1', 'APP-X', T.FEB_2, { asOf: T.FEB_2 });
    assert.equal(before.decision, Decision.DENY);
    assert.equal(before.reason, Reason.APPOINTMENT_NOT_YET_RECORDED);

    // 2月3日之后复核同一访问：任职、授权均已补齐
    const after = service.verifyAt('CRED-B1', 'APP-X', T.FEB_2, { asOf: T.FEB_3 });
    assert.equal(after.decision, Decision.ALLOW);

    // 最新视角直接放行
    assert.equal(service.verifyAt('CRED-B1', 'APP-X', T.FEB_15).decision, Decision.ALLOW);
  });

  test('授权登记晚于复核视角 -> AUTHORIZATION_NOT_YET_RECORDED', () => {
    const hr = new HrStore();
    const service = new AuthorizationService(hr, new Clock(T.FEB_15));
    hr.addAppointmentRecord({
      appointmentId: 'B-1', personId: 'P-200',
      startAt: T.FEB_1, endAt: T.JUN_30, revision: 1, recordedAt: T.FEB_1,
    });
    service.registerCredential({
      credentialId: 'CRED-B1', personId: 'P-200', appointmentId: 'B-1', appId: 'APP-X',
      issuedAt: T.FEB_1, expiresAt: T.JUN_30, recordedAt: T.FEB_1,
    });
    // 授权 2月3日才登记
    service.registerAuthorization({
      appointmentId: 'B-1', personId: 'P-200', appId: 'APP-X', recordedAt: T.FEB_3,
    });

    const r = service.verifyAt('CRED-B1', 'APP-X', T.FEB_2, { asOf: T.FEB_2 });
    assert.equal(r.decision, Decision.DENY);
    assert.equal(r.reason, Reason.AUTHORIZATION_NOT_YET_RECORDED);

    assert.equal(
      service.verifyAt('CRED-B1', 'APP-X', T.FEB_2, { asOf: T.FEB_3 }).decision,
      Decision.ALLOW,
    );
  });
});

describe('两次任职时间冲突 -> 人工核实', () => {
  test('重叠时段内任一张凭证都转 MANUAL_REVIEW，并带回冲突任职对', () => {
    const { service } = buildService();

    for (const credentialId of ['CRED-C1', 'CRED-C2']) {
      const r = service.verifyAt(credentialId, 'APP-X', T.APR_20);
      assert.equal(r.decision, Decision.MANUAL_REVIEW);
      assert.equal(r.reason, Reason.APPOINTMENT_OVERLAP);
      assert.equal(r.conflicts.length, 1);
      const ids = [
        r.conflicts[0].appointmentA.appointmentId,
        r.conflicts[0].appointmentB.appointmentId,
      ].sort();
      assert.deepEqual(ids, ['C-1', 'C-2']);
    }
  });

  test('半开边界：重叠左端点即人工核实，重叠结束后自动恢复判定', () => {
    const { service } = buildService();

    assert.equal(
      service.verifyAt('CRED-C2', 'APP-X', T.APR_1).decision,
      Decision.MANUAL_REVIEW,
    );
    // 4月30日 C-1 已结束（半开），只剩 C-2，自动放行
    assert.equal(service.verifyAt('CRED-C2', 'APP-X', T.APR_30).decision, Decision.ALLOW);
    // 重叠开始前只有 C-1
    assert.equal(service.verifyAt('CRED-C1', 'APP-X', T.MAR_20).decision, Decision.ALLOW);
  });

  test('人工核实优先于凭证过期：冲突期内即使凭证过期也先转人工', () => {
    const { service } = buildService();
    // 注册一张在冲突期内已过期的 C-1 凭证
    service.registerCredential({
      credentialId: 'CRED-C1-EXPIRED',
      personId: 'P-300', appointmentId: 'C-1', appId: 'APP-X',
      issuedAt: T.JAN_2, expiresAt: T.APR_10, recordedAt: T.JAN_2,
    });
    const r = service.verifyAt('CRED-C1-EXPIRED', 'APP-X', T.APR_20);
    assert.equal(r.decision, Decision.MANUAL_REVIEW);
    assert.equal(r.reason, Reason.APPOINTMENT_OVERLAP);
  });

  test('冲突经人事修订消除后，最新视角恢复自动放行，历史视角保留人工核实', () => {
    const hr = new HrStore();
    const service = new AuthorizationService(hr);
    hr.addAppointmentRecord({
      appointmentId: 'C-1', personId: 'P-300',
      startAt: T.JAN_1, endAt: T.APR_30, revision: 1, recordedAt: T.JAN_2,
    });
    hr.addAppointmentRecord({
      appointmentId: 'C-2', personId: 'P-300',
      startAt: T.APR_1, endAt: null, revision: 1, recordedAt: T.APR_1,
    });
    service.registerAuthorization({
      appointmentId: 'C-2', personId: 'P-300', appId: 'APP-X', recordedAt: T.APR_1,
    });
    service.registerCredential({
      credentialId: 'CRED-C2', personId: 'P-300', appointmentId: 'C-2', appId: 'APP-X',
      issuedAt: T.APR_1, expiresAt: T.FAR_FUTURE, recordedAt: T.APR_1,
    });

    assert.equal(
      service.verifyAt('CRED-C2', 'APP-X', T.APR_20).decision,
      Decision.MANUAL_REVIEW,
    );

    hr.addAppointmentRecord({
      appointmentId: 'C-1', personId: 'P-300',
      startAt: T.JAN_1, endAt: T.MAR_31, revision: 2, recordedAt: T.APR_25,
    });
    assert.equal(service.verifyAt('CRED-C2', 'APP-X', T.APR_20).decision, Decision.ALLOW);
    const oldView = service.verifyAt('CRED-C2', 'APP-X', T.APR_20, { asOf: T.APR_25 - 1 });
    assert.equal(oldView.decision, Decision.MANUAL_REVIEW);
  });
});

describe('凭证与授权归属校验', () => {
  test('未知凭证 ID -> CREDENTIAL_NOT_FOUND', () => {
    const { service } = buildService();
    const r = service.verifyAt('CRED-UNKNOWN', 'APP-X', T.FEB_15);
    assert.equal(r.decision, Decision.DENY);
    assert.equal(r.reason, Reason.CREDENTIAL_NOT_FOUND);
  });

  test('凭证工号与任职工号不一致 -> CREDENTIAL_PERSON_MISMATCH', () => {
    const { service } = buildService();
    const r = service.verifyAt({
      credentialId: 'FORGED',
      personId: 'P-999', // 任职 A-1 实际属于 P-100
      appointmentId: 'A-1', appId: 'APP-X',
      issuedAt: T.JAN_2, expiresAt: T.MAR_20, recordedAt: T.JAN_2,
    }, 'APP-X', T.FEB_15);
    assert.equal(r.decision, Decision.DENY);
    assert.equal(r.reason, Reason.CREDENTIAL_PERSON_MISMATCH);
  });

  test('凭证应用与目标应用不一致 -> CREDENTIAL_APP_MISMATCH', () => {
    const { service } = buildService();
    const r = service.verifyAt('CRED-A1', 'APP-Y', T.FEB_15);
    assert.equal(r.decision, Decision.DENY);
    assert.equal(r.reason, Reason.CREDENTIAL_APP_MISMATCH);
  });

  test('任职存在但缺少该应用授权 -> AUTHORIZATION_NOT_FOUND', () => {
    const { service } = buildService();
    // A-1 从未获得 APP-Z 授权
    const r = service.verifyAt({
      credentialId: 'CRED-A1-Z',
      personId: 'P-100', appointmentId: 'A-1', appId: 'APP-Z',
      issuedAt: T.JAN_2, expiresAt: T.MAR_20, recordedAt: T.JAN_2,
    }, 'APP-Z', T.FEB_15);
    assert.equal(r.decision, Decision.DENY);
    assert.equal(r.reason, Reason.AUTHORIZATION_NOT_FOUND);
  });

  test('视角早于凭证登记时间 -> CREDENTIAL_NOT_YET_ISSUED', () => {
    const { service } = buildService();
    const r = service.verifyAt('CRED-A1', 'APP-X', T.FEB_15, { asOf: T.JAN_1 });
    assert.equal(r.decision, Decision.DENY);
    assert.equal(r.reason, Reason.CREDENTIAL_NOT_YET_ISSUED);
  });

  test('凭证存在但任职编号在人事系统中不存在 -> APPOINTMENT_NOT_FOUND', () => {
    const { service } = buildService();
    const r = service.verifyAt({
      credentialId: 'CRED-GHOST',
      personId: 'P-100', appointmentId: 'A-GHOST', appId: 'APP-X',
      issuedAt: T.JAN_2, expiresAt: T.MAR_20, recordedAt: T.JAN_2,
    }, 'APP-X', T.FEB_15);
    assert.equal(r.decision, Decision.DENY);
    assert.equal(r.reason, Reason.APPOINTMENT_NOT_FOUND);
  });

  test('同一任职同一应用重复登记授权按最新登记幂等覆盖，不影响放行', () => {
    const { service } = buildService();
    service.registerAuthorization({
      appointmentId: 'A-1', personId: 'P-100', appId: 'APP-X', recordedAt: T.FEB_1,
    });
    assert.equal(service.verifyAt('CRED-A1', 'APP-X', T.FEB_15).decision, Decision.ALLOW);
  });
});

describe('verifyNow：当下可用性（固定时钟）', () => {
  test('时钟落在在任区间内 -> ALLOW；落在离任后 -> DENY', () => {
    const inJob = buildService(T.FEB_15);
    assert.equal(inJob.service.verifyNow('CRED-A1', 'APP-X').decision, Decision.ALLOW);

    const afterJob = buildService(T.APR_20);
    const r = afterJob.service.verifyNow('CRED-A1', 'APP-X');
    assert.equal(r.decision, Decision.DENY);
    assert.equal(r.reason, Reason.APPOINTMENT_NOT_ACTIVE);

    const rehired = buildService(T.MAY_15);
    assert.equal(rehired.service.verifyNow('CRED-A2', 'APP-X').decision, Decision.ALLOW);
    // 当下处于冲突期
    const c = new AuthorizationService(buildConflictStore(), new Clock(T.APR_20));
    registerConflictWorld(c);
    assert.equal(c.verifyNow('CRED-C1', 'APP-X').decision, Decision.MANUAL_REVIEW);
  });

  test('真实时钟分支：远未来到期的凭证在当前环境下仍可判定（不依赖系统时间构造数据）', () => {
    // 不注入固定时钟，走 Date.now()；凭证到期时间为 2099 年，任职在任
    const hr = new HrStore();
    const service = new AuthorizationService(hr);
    hr.addAppointmentRecord({
      appointmentId: 'A-2', personId: 'P-100',
      startAt: T.JAN_1, endAt: null, revision: 1, recordedAt: T.JAN_2,
    });
    service.registerAuthorization({
      appointmentId: 'A-2', personId: 'P-100', appId: 'APP-X', recordedAt: T.JAN_2,
    });
    service.registerCredential({
      credentialId: 'CRED-LONG', personId: 'P-100', appointmentId: 'A-2', appId: 'APP-X',
      issuedAt: T.JAN_2, expiresAt: T.FAR_FUTURE, recordedAt: T.JAN_2,
    });
    assert.equal(service.verifyNow('CRED-LONG', 'APP-X').decision, Decision.ALLOW);
  });
});

// --- 辅助：构造冲突世界用于 verifyNow 人工核实分支 ---
function buildConflictStore() {
  const hr = new HrStore();
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

function registerConflictWorld(service) {
  service.registerAuthorization({
    appointmentId: 'C-1', personId: 'P-300', appId: 'APP-X', recordedAt: T.JAN_2,
  });
  service.registerAuthorization({
    appointmentId: 'C-2', personId: 'P-300', appId: 'APP-X', recordedAt: T.APR_1,
  });
  service.registerCredential({
    credentialId: 'CRED-C1', personId: 'P-300', appointmentId: 'C-1', appId: 'APP-X',
    issuedAt: T.JAN_2, expiresAt: T.FAR_FUTURE, recordedAt: T.JAN_2,
  });
}
