import { test } from 'node:test';
import assert from 'node:assert/strict';

import { NetworkAccessController, POLICY } from '../src/nac.mjs';

const TTL = 5 * 60 * 1000;
const RECOVERY = ['remediation-portal', 'patch-server'];
const FULL_ITEMS = { osPatch: 'ok', antivirus: 'ok', diskEncryption: 'ok' };

/** 可手动推进的时钟，保证时间相关断言确定可复现。 */
function makeClock(start = 1_000_000) {
  const clock = { t: start };
  return {
    now: () => clock.t,
    advance: (ms) => {
      clock.t += ms;
    },
  };
}

function makeController(clock) {
  return new NetworkAccessController({
    reportTtlMs: TTL,
    maxClockSkewMs: 1000,
    recoveryServices: RECOVERY,
    now: clock.now,
  });
}

/** 走完“开会话 → 交报告 → 接入点确认”的完整合规流程。 */
function completeFlow(nac, deviceId, accessPointId, clock) {
  const session = nac.openSession({ deviceId, accessPointId });
  const report = nac.submitReport({
    deviceId,
    sessionId: session.sessionId,
    challenge: session.challenge,
    measuredAt: clock.now(),
    items: FULL_ITEMS,
  });
  assert.equal(report.accepted, true, `report rejected: ${report.reason}`);
  const confirm = nac.confirmAccessPoint({
    sessionId: session.sessionId,
    challenge: session.challenge,
    accessPointId,
  });
  assert.equal(confirm.accepted, true, `confirm rejected: ${confirm.reason}`);
  return session;
}

test('可接受结果：合规报告绑定当前挑战且测量新鲜，确认后输出正常策略', () => {
  const clock = makeClock();
  const nac = makeController(clock);

  const session = nac.openSession({ deviceId: 'dev-1', accessPointId: 'ap-1' });
  assert.equal(typeof session.challenge, 'string');
  assert.notEqual(session.challenge, session.sessionId);

  const accepted = nac.submitReport({
    deviceId: 'dev-1',
    sessionId: session.sessionId,
    challenge: session.challenge,
    measuredAt: clock.now(),
    items: FULL_ITEMS,
  });
  assert.deepEqual(accepted, { accepted: true });

  const confirm = nac.confirmAccessPoint({
    sessionId: session.sessionId,
    challenge: session.challenge,
    accessPointId: 'ap-1',
  });
  assert.deepEqual(confirm, { accepted: true });

  const decision = nac.evaluate({ deviceId: 'dev-1', sessionId: session.sessionId });
  assert.equal(decision.policy, POLICY.NORMAL);
  assert.deepEqual(decision.allowedServices, ['*']);
  assert.equal(decision.reason, 'ok');
});

test('应被拒绝：报告超期或缺项时降级为待核验，但仍可访问恢复服务', () => {
  const clock = makeClock();
  const nac = makeController(clock);

  // 场景一：测量时间超期
  const s1 = nac.openSession({ deviceId: 'dev-expired', accessPointId: 'ap-1' });
  const expired = nac.submitReport({
    deviceId: 'dev-expired',
    sessionId: s1.sessionId,
    challenge: s1.challenge,
    measuredAt: clock.now() - TTL - 1,
    items: FULL_ITEMS,
  });
  assert.deepEqual(expired, { accepted: false, reason: 'expired' });

  const d1 = nac.evaluate({ deviceId: 'dev-expired', sessionId: s1.sessionId });
  assert.equal(d1.policy, POLICY.PENDING_VERIFICATION);
  assert.equal(d1.reason, 'no-valid-report');
  assert.deepEqual(d1.allowedServices, RECOVERY);

  // 场景二：必选项缺失
  const s2 = nac.openSession({ deviceId: 'dev-incomplete', accessPointId: 'ap-1' });
  const incomplete = nac.submitReport({
    deviceId: 'dev-incomplete',
    sessionId: s2.sessionId,
    challenge: s2.challenge,
    measuredAt: clock.now(),
    items: { osPatch: 'ok' },
  });
  assert.equal(incomplete.accepted, false);
  assert.equal(incomplete.reason, 'incomplete-items');
  assert.deepEqual(incomplete.missing, ['antivirus', 'diskEncryption']);

  const d2 = nac.evaluate({ deviceId: 'dev-incomplete', sessionId: s2.sessionId });
  assert.equal(d2.policy, POLICY.PENDING_VERIFICATION);
  assert.deepEqual(d2.allowedServices, RECOVERY);

  // 场景三：挑战值不匹配（报告未绑定当前接入挑战）
  const s3 = nac.openSession({ deviceId: 'dev-challenge', accessPointId: 'ap-1' });
  const wrongChallenge = nac.submitReport({
    deviceId: 'dev-challenge',
    sessionId: s3.sessionId,
    challenge: 'stale-challenge',
    measuredAt: clock.now(),
    items: FULL_ITEMS,
  });
  assert.deepEqual(wrongChallenge, { accepted: false, reason: 'challenge-mismatch' });
  const d3 = nac.evaluate({ deviceId: 'dev-challenge', sessionId: s3.sessionId });
  assert.equal(d3.policy, POLICY.PENDING_VERIFICATION);
});

test('边界：测量时间恰好等于 TTL 可接受，超过 1ms 即拒绝；已接受报告随时间老化降级', () => {
  const clock = makeClock();
  const nac = makeController(clock);

  // 边界值：age === TTL 仍接受
  const s1 = nac.openSession({ deviceId: 'dev-edge', accessPointId: 'ap-1' });
  const atBoundary = nac.submitReport({
    deviceId: 'dev-edge',
    sessionId: s1.sessionId,
    challenge: s1.challenge,
    measuredAt: clock.now() - TTL,
    items: FULL_ITEMS,
  });
  assert.deepEqual(atBoundary, { accepted: true });

  // 未来测量（超出时钟容差）拒绝
  const s2 = nac.openSession({ deviceId: 'dev-future', accessPointId: 'ap-1' });
  const future = nac.submitReport({
    deviceId: 'dev-future',
    sessionId: s2.sessionId,
    challenge: s2.challenge,
    measuredAt: clock.now() + 60_000,
    items: FULL_ITEMS,
  });
  assert.deepEqual(future, { accepted: false, reason: 'future-measurement' });

  // 完整合规后为正常；推进时钟到恰好 TTL 仍正常，再超 1ms 降级待核验
  const session = completeFlow(nac, 'dev-aging', 'ap-1', clock);
  clock.advance(TTL);
  const fresh = nac.evaluate({ deviceId: 'dev-aging', sessionId: session.sessionId });
  assert.equal(fresh.policy, POLICY.NORMAL);

  clock.advance(1);
  const stale = nac.evaluate({ deviceId: 'dev-aging', sessionId: session.sessionId });
  assert.equal(stale.policy, POLICY.PENDING_VERIFICATION);
  assert.equal(stale.reason, 'report-expired');
  assert.deepEqual(stale.allowedServices, RECOVERY);
});

test('交错：换接入点后，旧点的迟到确认不能提升新会话权限', () => {
  const clock = makeClock();
  const nac = makeController(clock);

  // 设备在 ap-1 开会话并提交合规报告，但确认尚未到达
  const oldSession = nac.openSession({ deviceId: 'dev-roam', accessPointId: 'ap-1' });
  const report = nac.submitReport({
    deviceId: 'dev-roam',
    sessionId: oldSession.sessionId,
    challenge: oldSession.challenge,
    measuredAt: clock.now(),
    items: FULL_ITEMS,
  });
  assert.equal(report.accepted, true);

  // 设备换到 ap-2，产生全新会话与挑战值
  const newSession = nac.openSession({ deviceId: 'dev-roam', accessPointId: 'ap-2' });
  assert.notEqual(newSession.challenge, oldSession.challenge);

  // 旧接入点的迟到确认：被拒绝，且新会话不被提升
  const lateConfirm = nac.confirmAccessPoint({
    sessionId: oldSession.sessionId,
    challenge: oldSession.challenge,
    accessPointId: 'ap-1',
  });
  assert.deepEqual(lateConfirm, { accepted: false, reason: 'superseded-session' });

  const newDecision = nac.evaluate({ deviceId: 'dev-roam', sessionId: newSession.sessionId });
  assert.equal(newDecision.policy, POLICY.PENDING_VERIFICATION);
  assert.deepEqual(newDecision.allowedServices, RECOVERY);

  // 旧挑战值也不能重放到新会话
  const replay = nac.submitReport({
    deviceId: 'dev-roam',
    sessionId: newSession.sessionId,
    challenge: oldSession.challenge,
    measuredAt: clock.now(),
    items: FULL_ITEMS,
  });
  assert.deepEqual(replay, { accepted: false, reason: 'challenge-mismatch' });

  // 新会话按自身流程完成核验后才可正常
  const ownReport = nac.submitReport({
    deviceId: 'dev-roam',
    sessionId: newSession.sessionId,
    challenge: newSession.challenge,
    measuredAt: clock.now(),
    items: FULL_ITEMS,
  });
  assert.equal(ownReport.accepted, true);
  const ownConfirm = nac.confirmAccessPoint({
    sessionId: newSession.sessionId,
    challenge: newSession.challenge,
    accessPointId: 'ap-2',
  });
  assert.equal(ownConfirm.accepted, true);
  const finalDecision = nac.evaluate({ deviceId: 'dev-roam', sessionId: newSession.sessionId });
  assert.equal(finalDecision.policy, POLICY.NORMAL);
});

test('紧急冻结：新旧接入点都收敛到隔离；解冻后按全新会话办理', () => {
  const clock = makeClock();
  const nac = makeController(clock);

  // 同一设备在两个接入点各有一个已合规会话
  const s1 = completeFlow(nac, 'dev-freeze', 'ap-1', clock);
  const s2 = completeFlow(nac, 'dev-freeze', 'ap-2', clock);
  assert.equal(nac.evaluate({ deviceId: 'dev-freeze', sessionId: s1.sessionId }).policy, POLICY.NORMAL);
  assert.equal(nac.evaluate({ deviceId: 'dev-freeze', sessionId: s2.sessionId }).policy, POLICY.NORMAL);

  // 紧急冻结：两处都收敛到隔离，且不放行任何服务
  nac.freezeDevice('dev-freeze');
  for (const s of [s1, s2]) {
    const d = nac.evaluate({ deviceId: 'dev-freeze', sessionId: s.sessionId });
    assert.equal(d.policy, POLICY.QUARANTINE);
    assert.equal(d.reason, 'device-frozen');
    assert.deepEqual(d.allowedServices, []);
  }

  // 冻结期间报告与确认一律拒绝
  const frozenReport = nac.submitReport({
    deviceId: 'dev-freeze',
    sessionId: s2.sessionId,
    challenge: s2.challenge,
    measuredAt: clock.now(),
    items: FULL_ITEMS,
  });
  assert.deepEqual(frozenReport, { accepted: false, reason: 'device-frozen' });

  // 解冻：旧会话全部作废，评估仍为隔离，旧确认不再受理
  nac.unfreezeDevice('dev-freeze');
  for (const s of [s1, s2]) {
    const d = nac.evaluate({ deviceId: 'dev-freeze', sessionId: s.sessionId });
    assert.equal(d.policy, POLICY.QUARANTINE);
    assert.equal(d.reason, 'session-closed');
    const confirm = nac.confirmAccessPoint({
      sessionId: s.sessionId,
      challenge: s.challenge,
      accessPointId: s.accessPointId,
    });
    assert.deepEqual(confirm, { accepted: false, reason: 'unknown-session' });
  }

  // 恢复后按全新会话办理：先待核验（可走恢复服务），重新合规后正常
  const fresh = nac.openSession({ deviceId: 'dev-freeze', accessPointId: 'ap-1' });
  const pending = nac.evaluate({ deviceId: 'dev-freeze', sessionId: fresh.sessionId });
  assert.equal(pending.policy, POLICY.PENDING_VERIFICATION);
  assert.deepEqual(pending.allowedServices, RECOVERY);

  // 旧会话的挑战值无法重用到新会话
  const staleReplay = nac.submitReport({
    deviceId: 'dev-freeze',
    sessionId: fresh.sessionId,
    challenge: s1.challenge,
    measuredAt: clock.now(),
    items: FULL_ITEMS,
  });
  assert.deepEqual(staleReplay, { accepted: false, reason: 'challenge-mismatch' });

  const redone = nac.submitReport({
    deviceId: 'dev-freeze',
    sessionId: fresh.sessionId,
    challenge: fresh.challenge,
    measuredAt: clock.now(),
    items: FULL_ITEMS,
  });
  assert.equal(redone.accepted, true);
  const confirm = nac.confirmAccessPoint({
    sessionId: fresh.sessionId,
    challenge: fresh.challenge,
    accessPointId: 'ap-1',
  });
  assert.equal(confirm.accepted, true);
  const restored = nac.evaluate({ deviceId: 'dev-freeze', sessionId: fresh.sessionId });
  assert.equal(restored.policy, POLICY.NORMAL);
});
