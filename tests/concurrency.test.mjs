import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  PasskeyRegistrationService,
  LocalVerifier,
  makeVerifiedCredential,
  ErrorCodes,
  RegistrationStatus,
} from '../src/index.js';

/**
 * 多标签页 / 跨账户交错场景。
 * 服务的完成路径为同步原子操作，这里通过先并发“发起”再按顺序“回调”，
 * 确定性地复现两个标签页回调交错时的首绑竞争。
 */
function createService() {
  let tick = 0;
  const clock = () => `2026-11-19T00:00:${String(tick++).padStart(2, '0')}.000Z`;
  const service = new PasskeyRegistrationService({ verifier: new LocalVerifier(), clock });
  return { service, clock };
}

test('交错场景：同一凭据在两个账户间只首绑生效，败方另一挑战仍按自身状态成功', () => {
  const { service } = createService();

  // 两个标签页分别属于账户 A / B，各自持有一次性挑战。
  const sessionA = service.login('acct-A');
  const sessionB = service.login('acct-B');
  const regA = service.startRegistration({ accountSessionId: sessionA.sessionId, challenge: 'tab-A-1' });
  const regB1 = service.startRegistration({ accountSessionId: sessionB.sessionId, challenge: 'tab-B-1' });
  // B 还有第二个未完成挑战（另一标签页）。
  const regB2 = service.startRegistration({ accountSessionId: sessionB.sessionId, challenge: 'tab-B-2' });

  // 标签页 A 先回调，抢占共享凭据编号。
  const winA = service.completeRegistration({
    registrationId: regA.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'shared-key' }),
  });
  assert.equal(winA.status, RegistrationStatus.SUCCEEDED);

  // 标签页 B 随后回调同一凭据：必须被拒绝，且原因指向跨账户绑定。
  const loseB = service.completeRegistration({
    registrationId: regB1.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'shared-key' }),
  });
  assert.equal(loseB.status, RegistrationStatus.REJECTED);
  assert.equal(loseB.reason, ErrorCodes.CREDENTIAL_BOUND_TO_OTHER_ACCOUNT);
  assert.equal(loseB.details.existingAccountId, 'acct-A');

  // B 的另一个未完成挑战按自己的状态判断：换一个凭据即可成功。
  assert.equal(service.getRegistration(regB2.registrationId).status, RegistrationStatus.PENDING);
  const bOwn = service.completeRegistration({
    registrationId: regB2.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'b-only-key' }),
  });
  assert.equal(bOwn.status, RegistrationStatus.SUCCEEDED);

  // 全局只有两条绑定，各归其主；失败登记不产生绑定。
  assert.deepEqual(
    service.listCredentials('acct-A').map((c) => c.credentialId),
    ['shared-key'],
  );
  assert.deepEqual(
    service.listCredentials('acct-B').map((c) => c.credentialId),
    ['b-only-key'],
  );

  // 每个账户看到的登记历史包含各自的成功与拒绝状态。
  const historyB = service.listRegistrations('acct-B');
  assert.deepEqual(
    historyB.map((r) => r.status),
    [RegistrationStatus.REJECTED, RegistrationStatus.SUCCEEDED],
  );
});

test('交错场景：同一账户两个标签页抢同一凭据，先完成者生效，后完成者拒绝且可回放', () => {
  const { service } = createService();
  const session = service.login('acct-1');
  const tab1 = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'tab-1' });
  const tab2 = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'tab-2' });

  const r1 = service.completeRegistration({
    registrationId: tab1.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'race-cred' }),
  });
  const r2 = service.completeRegistration({
    registrationId: tab2.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'race-cred' }),
  });

  assert.equal(r1.status, RegistrationStatus.SUCCEEDED);
  assert.equal(r2.status, RegistrationStatus.REJECTED);
  assert.equal(r2.reason, ErrorCodes.CREDENTIAL_ALREADY_BOUND);

  // 败方重复回调仍回放原拒绝结果；胜方重复回调仍回放成功结果。
  const r2Again = service.completeRegistration({
    registrationId: tab2.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'race-cred' }),
  });
  assert.equal(r2Again.replay, true);
  assert.equal(r2Again.status, RegistrationStatus.REJECTED);
  assert.equal(r2Again.reason, ErrorCodes.CREDENTIAL_ALREADY_BOUND);

  const r1Again = service.completeRegistration({
    registrationId: tab1.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'race-cred' }),
  });
  assert.equal(r1Again.replay, true);
  assert.equal(r1Again.status, RegistrationStatus.SUCCEEDED);

  assert.equal(service.listCredentials('acct-1').length, 1);
});

test('交错场景：冻结一个标签页不影响另一标签页未完成挑战的独立判断', () => {
  const { service } = createService();
  const session = service.login('acct-1');
  const tab1 = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'freeze-tab' });
  const tab2 = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'live-tab' });

  service.freezeRegistration(tab1.registrationId);
  assert.equal(service.getRegistration(tab2.registrationId).status, RegistrationStatus.PENDING);

  const blocked = service.completeRegistration({
    registrationId: tab1.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'cred-tab1' }),
  });
  assert.equal(blocked.status, RegistrationStatus.REJECTED);
  assert.equal(blocked.reason, ErrorCodes.REGISTRATION_FROZEN);

  const alive = service.completeRegistration({
    registrationId: tab2.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'cred-tab2' }),
  });
  assert.equal(alive.status, RegistrationStatus.SUCCEEDED);

  assert.deepEqual(
    service.listRegistrations('acct-1').map((r) => r.status),
    [RegistrationStatus.REJECTED, RegistrationStatus.SUCCEEDED],
  );
});

test('交错场景：会话替换与冻结同时存在时，各登记按自身绑定状态分别裁决', () => {
  const { service } = createService();
  const oldSession = service.login('acct-1');
  const regOnOldSession = service.startRegistration({
    accountSessionId: oldSession.sessionId,
    challenge: 'old-session-reg',
  });
  const newSession = service.replaceAccountSession('acct-1');
  const regOnNewSession = service.startRegistration({
    accountSessionId: newSession.sessionId,
    challenge: 'new-session-reg',
  });
  service.freezeRegistration(regOnNewSession.registrationId);

  const oldResult = service.completeRegistration({
    registrationId: regOnOldSession.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'cred-old' }),
  });
  const newResult = service.completeRegistration({
    registrationId: regOnNewSession.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'cred-new' }),
  });

  assert.equal(oldResult.reason, ErrorCodes.SESSION_REPLACED);
  assert.equal(newResult.reason, ErrorCodes.REGISTRATION_FROZEN);
  assert.equal(service.listCredentials('acct-1').length, 0);

  // 解冻不存在（冻结为终态前的安全态），新会话重新发起一条登记仍可成功，证明账户未被锁死。
  const regAgain = service.startRegistration({ accountSessionId: newSession.sessionId, challenge: 'fresh-reg' });
  const ok = service.completeRegistration({
    registrationId: regAgain.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'cred-fresh' }),
  });
  assert.equal(ok.status, RegistrationStatus.SUCCEEDED);
});

test('查询面：凭据列表仅含成功绑定，登记列表覆盖 pending/frozen/rejected/succeeded 全状态', () => {
  const { service } = createService();
  const session = service.login('acct-q');
  const pending = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'q-pending' });
  const frozen = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'q-frozen' });
  service.freezeRegistration(frozen.registrationId);
  const rejected = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'q-rejected' });
  service.completeRegistration({ registrationId: rejected.registrationId, credential: { bogus: true } });
  const succeeded = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'q-ok' });
  service.completeRegistration({
    registrationId: succeeded.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'cred-q' }),
  });

  const statuses = Object.fromEntries(
    service.listRegistrations('acct-q').map((r) => [r.challenge, r.status]),
  );
  assert.equal(statuses['q-pending'], RegistrationStatus.PENDING);
  assert.equal(statuses['q-frozen'], RegistrationStatus.FROZEN);
  assert.equal(statuses['q-rejected'], RegistrationStatus.REJECTED);
  assert.equal(statuses['q-ok'], RegistrationStatus.SUCCEEDED);

  assert.deepEqual(
    service.listCredentials('acct-q').map((c) => c.credentialId),
    ['cred-q'],
  );
  // 外部拿到的是快照，改动不影响服务内部状态。
  const snapshot = service.listCredentials('acct-q');
  snapshot[0].credentialId = 'tampered';
  assert.equal(service.listCredentials('acct-q')[0].credentialId, 'cred-q');
  assert.equal(service.getRegistration(pending.registrationId).status, RegistrationStatus.PENDING);
});
