import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';

import {
  SessionContinuationService,
  SimulatedAuthDevice,
  createAuthEvent,
  createPresenceEvent,
  TERMINAL_STATUS,
  SESSION_STATUS,
  ORDERING,
} from '../index.js';

/** 可手动推进的虚拟时钟，避免对真实时间产生依赖。 */
function virtualClock(start = 1_000) {
  let now = start;
  const clock = () => now;
  clock.advance = (ms) => {
    now += ms;
    return now;
  };
  clock.set = (t) => {
    now = t;
    return now;
  };
  return clock;
}

function makeService({ gracePeriodMs = 60_000, ordering = ORDERING.SEQUENCE, clock = virtualClock() } = {}) {
  return {
    clock,
    device: new SimulatedAuthDevice({ clock }),
    service: new SessionContinuationService({ gracePeriodMs, ordering, clock }),
  };
}

describe('接续认证：正常生命周期与宽限恢复', () => {
  let clock;
  let device;
  let service;

  beforeEach(() => {
    ({ clock, device, service } = makeService());
  });

  test('认证确认开会话，离场即锁屏，宽限内同成员认证恢复同一会话', () => {
    const r1 = service.handleEvent(device.authenticate('alice', 'T1'));
    assert.equal(r1.status, 'applied');
    assert.equal(r1.action, 'session-started');
    const sessionId = r1.sessionId;

    let state = service.getTerminalState('T1');
    assert.equal(state.status, TERMINAL_STATUS.ACTIVE);
    assert.equal(state.currentSessionId, sessionId);

    service.handleEvent(device.enter('alice', 'T1'));
    assert.deepEqual(service.getPresentMembers('T1'), ['alice']);

    const r2 = service.handleEvent(device.leave('alice', 'T1'));
    assert.equal(r2.action, 'locked-on-leave');
    state = service.getTerminalState('T1');
    assert.equal(state.status, TERMINAL_STATUS.LOCKED);
    assert.equal(state.currentSessionId, sessionId, '锁屏期间会话仍归属原成员');

    clock.advance(30_000);
    const r3 = service.handleEvent(device.authenticate('alice', 'T1'));
    assert.equal(r3.action, 'session-resumed');
    assert.equal(r3.sessionId, sessionId, '宽限内恢复的是原会话');
    state = service.getTerminalState('T1');
    assert.equal(state.status, TERMINAL_STATUS.ACTIVE);

    const session = service.getCurrentSession('T1');
    assert.equal(session.status, SESSION_STATUS.ACTIVE);
    assert.equal(session.resumeCount, 1);
    assert.equal(session.resumedAt, clock());
  });

  test('宽限边界：恰好等于宽限期可恢复，超过 1ms 则另开新会话', () => {
    const start = service.handleEvent(device.authenticate('alice', 'T1')).sessionId;
    service.handleEvent(device.leave('alice', 'T1'));
    const lockedAt = service.getTerminalState('T1').lockedAt;

    clock.set(lockedAt + 60_000);
    const resumed = service.handleEvent(device.authenticate('alice', 'T1'));
    assert.equal(resumed.action, 'session-resumed');
    assert.equal(resumed.sessionId, start);

    // 再锁一次，这次越过宽限
    service.handleEvent(device.leave('alice', 'T1'));
    const lockedAt2 = service.getTerminalState('T1').lockedAt;
    clock.set(lockedAt2 + 60_001);
    const r = service.handleEvent(device.authenticate('alice', 'T1'));
    assert.equal(r.action, 'new-session-after-grace');
    assert.notEqual(r.sessionId, start);

    const old = service.getSession('T1', start);
    assert.equal(old.status, SESSION_STATUS.ENDED);
    assert.equal(old.endReason, 'grace-expired');
    assert.equal(service.getTerminalState('T1').status, TERMINAL_STATUS.ACTIVE);
  });

  test('sweepExpired 只清理超过宽限的锁屏终端，未超宽限保持锁屏', () => {
    service.handleEvent(device.authenticate('alice', 'T1'));
    service.handleEvent(device.leave('alice', 'T1'));
    const sid = service.getTerminalState('T1').currentSessionId;

    clock.advance(30_000);
    assert.deepEqual(service.sweepExpired(), []);
    assert.equal(service.getTerminalState('T1').status, TERMINAL_STATUS.LOCKED);

    clock.advance(30_001);
    const expired = service.sweepExpired();
    assert.deepEqual(expired, [sid]);
    assert.equal(service.getTerminalState('T1').status, TERMINAL_STATUS.IDLE);
    assert.equal(service.getTerminalState('T1').currentSessionId, null);
    assert.equal(service.getSession('T1', sid).endReason, 'grace-expired');

    // idle 后同成员认证产生全新会话
    const r = service.handleEvent(device.authenticate('alice', 'T1'));
    assert.equal(r.action, 'session-started');
    assert.notEqual(r.sessionId, sid);
  });

  test('宽限期为 0 时锁屏后立即无法恢复原会话', () => {
    ({ clock, device, service } = makeService({ gracePeriodMs: 0 }));
    const start = service.handleEvent(device.authenticate('alice', 'T1')).sessionId;
    service.handleEvent(device.leave('alice', 'T1'));
    clock.advance(1);
    const r = service.handleEvent(device.authenticate('alice', 'T1'));
    assert.equal(r.action, 'new-session-after-grace');
    assert.notEqual(r.sessionId, start);
  });
});

describe('接续认证：成员接手与迟到旧确认', () => {
  let clock;
  let device;
  let service;

  beforeEach(() => {
    ({ clock, device, service } = makeService({ ordering: ORDERING.WATERMARK }));
  });

  test('活跃期间另一成员确认接手：旧会话结束、新会话归属新成员', () => {
    const a = service.handleEvent(device.authenticate('alice', 'T1')).sessionId;
    const r = service.handleEvent(device.authenticate('bob', 'T1'));
    assert.equal(r.action, 'takeover-session-started');
    assert.notEqual(r.sessionId, a);

    const old = service.getSession('T1', a);
    assert.equal(old.status, SESSION_STATUS.ENDED);
    assert.equal(old.endReason, 'taken-over');

    const state = service.getTerminalState('T1');
    assert.equal(state.status, TERMINAL_STATUS.ACTIVE);
    assert.equal(state.currentSessionId, r.sessionId);
    assert.equal(service.getCurrentSession('T1').memberId, 'bob');
  });

  test('锁屏期间另一成员确认接手，终端进入该成员的新会话', () => {
    const a = service.handleEvent(device.authenticate('alice', 'T1')).sessionId;
    service.handleEvent(device.leave('alice', 'T1'));
    clock.advance(5_000);
    const r = service.handleEvent(device.authenticate('bob', 'T1'));
    assert.equal(r.action, 'takeover-session-started');
    assert.equal(service.getCurrentSession('T1').memberId, 'bob');
    assert.equal(service.getSession('T1', a).endReason, 'taken-over');
    assert.equal(service.getTerminalState('T1').status, TERMINAL_STATUS.ACTIVE);
  });

  test('接手后晚到的旧用户确认留在旧记录中，当前用屏人继续使用自己的会话', () => {
    const a = service.handleEvent(device.authenticate('alice', 'T1')).sessionId; // accessNo 1
    service.handleEvent(device.leave('alice', 'T1')); // 2
    const b = service.handleEvent(device.authenticate('bob', 'T1')).sessionId; // 3 接手

    // 模拟旧用户确认（设备侧编号 2 早于接手事件 3）因网络乱序晚到
    const lateAlice = createAuthEvent({
      memberId: 'alice',
      terminalId: 'T1',
      timestamp: 500,
      accessNo: 2,
    });
    const late = service.handleEvent(lateAlice);
    assert.equal(late.status, 'stale');
    assert.equal(late.action, 'stale-ignored');
    assert.equal(late.sessionId, a, '迟到确认归档到 alice 的旧会话');

    // 当前用屏人不受影响
    const state = service.getTerminalState('T1');
    assert.equal(state.status, TERMINAL_STATUS.ACTIVE);
    assert.equal(state.currentSessionId, b);
    assert.equal(state.lastAccessNo, 3, '水位不回退');

    // 旧会话记录里有迟到条目，新会话记录里没有
    const oldSession = service.getSession('T1', a);
    assert.ok(oldSession.log.some((e) => e.action === 'stale-ignored' && e.accessNo === 2));
    const newSession = service.getSession('T1', b);
    assert.equal(newSession.log.some((e) => e.accessNo === 2), false);

    // bob 仍可正常确认、锁屏、恢复
    service.handleEvent(device.leave('bob', 'T1'));
    clock.advance(2_000);
    const resume = service.handleEvent(device.authenticate('bob', 'T1'));
    assert.equal(resume.action, 'session-resumed');
    assert.equal(resume.sessionId, b);
  });

  test('迟到的旧进出报告同样只入旧记录，不触发锁屏或解锁', () => {
    const a = service.handleEvent(device.authenticate('alice', 'T1')).sessionId; // accessNo 1
    const b = service.handleEvent(device.authenticate('bob', 'T1')).sessionId; // accessNo 2

    // 设备侧旧编号（低于当前水位 2）的离场报告晚到
    const lateLeave = createPresenceEvent({
      memberId: 'alice',
      terminalId: 'T1',
      timestamp: 400,
      accessNo: 1,
      status: 'out',
    });
    const r = service.handleEvent(lateLeave);
    assert.equal(r.status, 'stale');
    assert.equal(r.sessionId, a);
    assert.equal(service.getTerminalState('T1').status, TERMINAL_STATUS.ACTIVE);
    assert.equal(service.getTerminalState('T1').currentSessionId, b);
  });

  test('审计轨迹按接入编号排序，迟到事件与接手事件可区分核对', () => {
    service.handleEvent(device.authenticate('alice', 'T1')); // 1
    service.handleEvent(device.leave('alice', 'T1')); // 2
    service.handleEvent(device.authenticate('bob', 'T1')); // 3
    const late = service.handleEvent(
      createAuthEvent({ memberId: 'alice', terminalId: 'T1', timestamp: 10, accessNo: 2 }),
    );

    const trail = service.getAuditTrail('T1');
    assert.deepEqual(
      trail.map((e) => e.accessNo),
      [1, 2, 2, 3],
    );
    const staleEntry = trail.filter((e) => e.accessNo === 2).find((e) => e.action === 'stale-ignored');
    assert.ok(staleEntry);
    assert.equal(staleEntry.sessionId, 'T1#1');
  });
});

describe('接续认证：sequence 模式缺口缓冲与回填', () => {
  let clock;
  let device;
  let service;

  beforeEach(() => {
    ({ clock, device, service } = makeService());
  });

  test('乱序到达：缺口先缓冲，补齐后按序连续回填', () => {
    // 手工指定接入编号，模拟设备侧顺序为 1、2、3 但网络乱序送达
    const e1 = createAuthEvent({ memberId: 'alice', terminalId: 'T1', timestamp: 100, accessNo: 1 });
    const e2 = createPresenceEvent({
      memberId: 'alice',
      terminalId: 'T1',
      timestamp: 200,
      accessNo: 2,
      status: 'in',
    });
    const e3 = createPresenceEvent({
      memberId: 'alice',
      terminalId: 'T1',
      timestamp: 300,
      accessNo: 3,
      status: 'out',
    });

    const r3 = service.handleEvent(e3); // 先到 3
    assert.equal(r3.status, 'buffered');
    assert.equal(r3.waitingFor, 1);
    assert.deepEqual(service.getTerminalState('T1').pendingAccessNos, [3]);

    const r2 = service.handleEvent(e2); // 再到 2，仍缺 1
    assert.equal(r2.status, 'buffered');
    assert.deepEqual(service.getTerminalState('T1').pendingAccessNos, [2, 3]);
    assert.equal(service.getTerminalState('T1').status, TERMINAL_STATUS.IDLE, '缓冲期间状态不变');

    const r1 = service.handleEvent(e1); // 最后到 1，触发 2、3 回填
    assert.equal(r1.status, 'applied');
    assert.equal(r1.action, 'session-started');
    assert.deepEqual(
      r1.drained.map((d) => d.accessNo),
      [2, 3],
    );
    assert.deepEqual(
      r1.drained.map((d) => d.action),
      ['presence-in-active', 'locked-on-leave'],
    );
    assert.equal(service.getTerminalState('T1').status, TERMINAL_STATUS.LOCKED);
    assert.deepEqual(service.getTerminalState('T1').pendingAccessNos, []);

    // 回填后的锁屏仍可在宽限内恢复（设备编号继续为 4）
    clock.advance(10_000);
    const resume = service.handleEvent(
      createAuthEvent({ memberId: 'alice', terminalId: 'T1', timestamp: clock(), accessNo: 4 }),
    );
    assert.equal(resume.action, 'session-resumed');
  });

  test('同编号幂等重发被忽略，同编号不同载荷被拒绝', () => {
    const e1 = createAuthEvent({ memberId: 'alice', terminalId: 'T1', timestamp: 100, accessNo: 1 });
    service.handleEvent(e1);
    const dup = service.handleEvent({ ...e1 });
    assert.equal(dup.status, 'duplicate');
    assert.equal(service.getTerminalState('T1').lastAccessNo, 1);

    const conflict = createAuthEvent({
      memberId: 'mallory',
      terminalId: 'T1',
      timestamp: e1.timestamp,
      accessNo: 1,
    });
    const r = service.handleEvent(conflict);
    assert.equal(r.status, 'rejected');
    assert.equal(r.reason, 'access-no-conflict');
    assert.equal(service.getCurrentSession('T1').memberId, 'alice', '冲突事件不改变状态');
  });

  test('缓冲中的同编号事件同样区分重发与冲突', () => {
    const e1 = createAuthEvent({ memberId: 'alice', terminalId: 'T1', timestamp: 100, accessNo: 1 });
    const e2 = createPresenceEvent({
      memberId: 'alice',
      terminalId: 'T1',
      timestamp: 200,
      accessNo: 2,
      status: 'out',
    });
    const r2 = service.handleEvent(e2);
    assert.equal(r2.status, 'buffered');

    assert.equal(service.handleEvent({ ...e2 }).status, 'duplicate');
    const conflict = createPresenceEvent({
      memberId: 'bob',
      terminalId: 'T1',
      timestamp: e2.timestamp,
      accessNo: 2,
      status: 'out',
    });
    assert.equal(service.handleEvent(conflict).status, 'rejected');

    const r1 = service.handleEvent(e1);
    assert.equal(r1.status, 'applied');
    assert.equal(r1.drained.length, 1);
  });
});

describe('接续认证：边界与防御性校验', () => {
  test('无会话时的进出报告进入孤儿记录，不改变 idle 状态', () => {
    const { device, service } = makeService();
    const r = service.handleEvent(device.enter('alice', 'T1'));
    assert.equal(r.status, 'applied');
    assert.equal(r.action, 'presence-in-recorded');
    assert.equal(r.sessionId, null);
    assert.equal(service.getTerminalState('T1').status, TERMINAL_STATUS.IDLE);
    assert.deepEqual(service.getPresentMembers('T1'), ['alice']);
    const trail = service.getAuditTrail('T1');
    assert.equal(trail.length, 1);
    assert.equal(trail[0].sessionId, null);
  });

  test('他人离场报告只记录，不会锁掉当前用屏人的会话', () => {
    const { device, service } = makeService();
    service.handleEvent(device.authenticate('alice', 'T1'));
    service.handleEvent(device.enter('alice', 'T1'));
    service.handleEvent(device.enter('bob', 'T1'));
    assert.deepEqual(service.getPresentMembers('T1'), ['alice', 'bob']);
    const r = service.handleEvent(device.leave('bob', 'T1'));
    assert.equal(r.action, 'presence-out-recorded');
    assert.equal(service.getTerminalState('T1').status, TERMINAL_STATUS.ACTIVE);
    assert.deepEqual(service.getPresentMembers('T1'), ['alice']);
  });

  test('同成员重复认证确认重affirm，不另开会话', () => {
    const { device, service } = makeService();
    const a = service.handleEvent(device.authenticate('alice', 'T1')).sessionId;
    const r = service.handleEvent(device.authenticate('alice', 'T1'));
    assert.equal(r.action, 'auth-reaffirmed');
    assert.equal(r.sessionId, a);
    assert.equal(service.listSessions('T1').length, 1);
  });

  test('多终端状态与接入编号相互独立', () => {
    const { device, service } = makeService();
    service.handleEvent(device.authenticate('alice', 'T1'));
    service.handleEvent(device.authenticate('alice', 'T2'));
    assert.equal(service.getTerminalState('T1').lastAccessNo, 1);
    assert.equal(service.getTerminalState('T2').lastAccessNo, 1);

    service.handleEvent(device.leave('alice', 'T1'));
    assert.equal(service.getTerminalState('T1').status, TERMINAL_STATUS.LOCKED);
    assert.equal(service.getTerminalState('T2').status, TERMINAL_STATUS.ACTIVE);
    assert.notEqual(
      service.getTerminalState('T1').currentSessionId,
      service.getTerminalState('T2').currentSessionId,
    );
  });

  test('未知终端的查询返回安全空值', () => {
    const { service } = makeService();
    assert.equal(service.getTerminalState('nope'), null);
    assert.equal(service.getCurrentSession('nope'), null);
    assert.deepEqual(service.getAuditTrail('nope'), []);
    assert.deepEqual(service.listSessions('nope'), []);
  });

  test('查询返回深拷贝，外部无法篡改内部状态', () => {
    const { device, service } = makeService();
    service.handleEvent(device.authenticate('alice', 'T1'));
    const state = service.getTerminalState('T1');
    state.status = 'hacked';
    assert.equal(service.getTerminalState('T1').status, TERMINAL_STATUS.ACTIVE);

    const session = service.getCurrentSession('T1');
    session.memberId = 'mallory';
    assert.equal(service.getCurrentSession('T1').memberId, 'alice');
  });

  test('非法事件抛出 TypeError 并聚合字段原因', () => {
    assert.throws(() => makeService().service.handleEvent(null), TypeError);
    assert.throws(
      () =>
        makeService().service.handleEvent({
          type: 'auth',
          memberId: '',
          terminalId: 'T1',
          timestamp: -1,
          accessNo: 0,
        }),
      /memberId.*timestamp.*accessNo/s,
    );
    assert.throws(
      () =>
        makeService().service.handleEvent({
          type: 'presence',
          memberId: 'alice',
          terminalId: 'T1',
          timestamp: 1,
          accessNo: 1,
          status: 'maybe',
        }),
      /status/,
    );
  });

  test('非法构造参数与非法 sweep 时间抛出 TypeError', () => {
    assert.throws(() => new SessionContinuationService({ gracePeriodMs: -1 }), TypeError);
    assert.throws(() => new SessionContinuationService({ ordering: 'bogus' }), TypeError);
    assert.throws(() => new SessionContinuationService({ clock: 123 }), TypeError);
    const { service } = makeService();
    assert.throws(() => service.sweepExpired(-5), TypeError);
  });

  test('watermark 模式：历史窗口事件按重发/迟到归档，迟到确认不得接手终端', () => {
    const { device, service } = makeService({ ordering: ORDERING.WATERMARK });
    const e1 = device.authenticate('alice', 'T1'); // accessNo 1，应用后水位=1
    service.handleEvent(e1);

    // 同载荷重发 -> duplicate
    assert.equal(service.handleEvent({ ...e1 }).status, 'duplicate');

    // 低于水位的异载荷事件（旧窗口迟到）-> stale，不改状态
    const stalePresence = service.handleEvent(
      createPresenceEvent({
        memberId: 'alice',
        terminalId: 'T1',
        timestamp: 50,
        accessNo: 1,
        status: 'out',
      }),
    );
    assert.equal(stalePresence.status, 'stale');
    // 迟到事件本身也支持幂等重发
    assert.equal(
      service.handleEvent(
        createPresenceEvent({
          memberId: 'alice',
          terminalId: 'T1',
          timestamp: 50,
          accessNo: 1,
          status: 'out',
        }),
      ).status,
      'duplicate',
    );
    assert.equal(service.getCurrentSession('T1').memberId, 'alice');

    // 水位正常推进到 3：1 号认证、2 号进入报告、3 号再次确认
    service.handleEvent(device.enter('alice', 'T1')); // accessNo 2
    const reaffirm = service.handleEvent(device.authenticate('alice', 'T1')); // accessNo 3
    assert.equal(reaffirm.status, 'applied');
    assert.equal(service.getTerminalState('T1').lastAccessNo, 3);

    // 迟到的缺口编号（设备侧从未送达、编号低于水位）-> stale，即便来自另一成员也不能接手
    const lateTwo = service.handleEvent(
      createAuthEvent({ memberId: 'bob', terminalId: 'T1', timestamp: 150, accessNo: 2 }),
    );
    assert.equal(lateTwo.status, 'stale');
    assert.equal(service.getCurrentSession('T1').memberId, 'alice');
    assert.equal(service.getTerminalState('T1').status, TERMINAL_STATUS.ACTIVE);
    assert.equal(service.getTerminalState('T1').lastAccessNo, 3, '水位不回退');

    // 而编号 4 的真实新事件（水位+1）正常应用，bob 在此刻才能接手
    const takeover = service.handleEvent(
      createAuthEvent({ memberId: 'bob', terminalId: 'T1', timestamp: 250, accessNo: 4 }),
    );
    assert.equal(takeover.action, 'takeover-session-started');
    assert.equal(service.getCurrentSession('T1').memberId, 'bob');
  });

  test('watermark 模式同样支持离场锁屏与宽限内恢复', () => {
    const { clock, device, service } = makeService({ ordering: ORDERING.WATERMARK });
    const sid = service.handleEvent(device.authenticate('alice', 'T1')).sessionId;
    service.handleEvent(device.leave('alice', 'T1'));
    assert.equal(service.getTerminalState('T1').status, TERMINAL_STATUS.LOCKED);

    clock.advance(10_000);
    const resume = service.handleEvent(device.authenticate('alice', 'T1'));
    assert.equal(resume.action, 'session-resumed');
    assert.equal(resume.sessionId, sid);
  });
});

describe('接续认证：完整交错剧情', () => {
  test('锁屏-恢复-再锁屏-他人接手-迟到确认归档 的端到端剧情', () => {
    const clock = virtualClock();
    const device = new SimulatedAuthDevice({ clock });
    const service = new SessionContinuationService({
      gracePeriodMs: 60_000,
      ordering: ORDERING.WATERMARK,
      clock,
    });

    const a = service.handleEvent(device.authenticate('alice', 'T1')).sessionId;
    service.handleEvent(device.enter('alice', 'T1'));
    service.handleEvent(device.leave('alice', 'T1')); // 锁屏
    clock.advance(20_000);
    const resume = service.handleEvent(device.authenticate('alice', 'T1'));
    assert.equal(resume.sessionId, a);

    service.handleEvent(device.leave('alice', 'T1')); // 再次锁屏
    clock.advance(70_000); // 超过宽限
    const takeover = service.handleEvent(device.authenticate('bob', 'T1'));
    assert.equal(takeover.action, 'takeover-session-started');

    // 旧会话在 bob 接手时被结束（即便此前未 sweep）
    assert.equal(service.getSession('T1', a).endReason, 'taken-over');
    assert.equal(service.getCurrentSession('T1').memberId, 'bob');

    // 晚到的 alice 旧确认：设备侧编号 3，低于当前水位 6 且从未送达过
    const late = service.handleEvent(
      createAuthEvent({ memberId: 'alice', terminalId: 'T1', timestamp: 5, accessNo: 3 }),
    );
    assert.equal(late.status, 'stale');
    assert.equal(late.sessionId, a);
    assert.equal(service.getTerminalState('T1').currentSessionId, takeover.sessionId);

    // 旧会话记录含全部 alice 事件且终态为 ended
    const old = service.getSession('T1', a);
    assert.equal(old.status, SESSION_STATUS.ENDED);
    // 5 条已应用事件 + 1 条迟到确认
    assert.equal(old.log.length, 6);
    assert.equal(old.log.at(-1).action, 'stale-ignored');
  });
});
