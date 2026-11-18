import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  PasskeyRegistrationService,
  LocalVerifier,
  TrustedCredential,
  makeVerifiedCredential,
  RegistrationError,
  ErrorCodes,
  RegistrationStatus,
} from '../src/index.js';

/**
 * 构造带确定性时钟的服务，避免同毫秒时间戳造成排序不稳定。
 */
function createService() {
  let tick = 0;
  const clock = () => `2026-11-19T00:00:${String(tick++).padStart(2, '0')}.000Z`;
  const verifier = new LocalVerifier();
  const service = new PasskeyRegistrationService({ verifier, clock });
  return { service, verifier, clock };
}

test('可接受结果：已登录账户凭一次性挑战完成登记，凭据进入可用列表', () => {
  const { service } = createService();
  const session = service.login('acct-1');
  const reg = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'challenge-1' });
  assert.equal(reg.status, RegistrationStatus.PENDING);

  const credential = makeVerifiedCredential({
    credentialId: 'cred-1',
    publicKey: 'pk-1',
    counter: 0,
    transports: ['usb', 'nfc'],
    deviceLabel: 'YubiKey 5',
  });
  const result = service.completeRegistration({ registrationId: reg.registrationId, credential });

  assert.equal(result.status, RegistrationStatus.SUCCEEDED);
  assert.equal(result.replay, false);
  assert.equal(result.credential.credentialId, 'cred-1');
  assert.equal(result.credential.accountId, 'acct-1');

  const list = service.listCredentials('acct-1');
  assert.equal(list.length, 1);
  assert.equal(list[0].credentialId, 'cred-1');

  const history = service.listRegistrations('acct-1');
  assert.equal(history.length, 1);
  assert.equal(history[0].status, RegistrationStatus.SUCCEEDED);
});

test('应被阻止：普通对象伪装的验签结果不得完成登记', () => {
  const { service } = createService();
  const session = service.login('acct-1');
  const reg = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'challenge-plain' });

  const fake = { credentialId: 'cred-fake', publicKey: 'pk' };
  const result = service.completeRegistration({ registrationId: reg.registrationId, credential: fake });

  assert.equal(result.status, RegistrationStatus.REJECTED);
  assert.equal(result.reason, ErrorCodes.NOT_TRUSTED_CREDENTIAL);
  assert.equal(service.listCredentials('acct-1').length, 0);
  assert.equal(service.getRegistration(reg.registrationId).status, RegistrationStatus.REJECTED);
});

test('应被阻止：一次性挑战不得在不同登记中重复签发', () => {
  const { service } = createService();
  const session = service.login('acct-1');
  service.startRegistration({ accountSessionId: session.sessionId, challenge: 'once-only' });

  assert.throws(
    () => service.startRegistration({ accountSessionId: session.sessionId, challenge: 'once-only' }),
    (err) => err instanceof RegistrationError && err.code === ErrorCodes.CHALLENGE_ALREADY_USED,
  );
});

test('应被阻止：冻结的登记会话不能完成登记', () => {
  const { service } = createService();
  const session = service.login('acct-1');
  const reg = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'challenge-frozen' });

  const frozen = service.freezeRegistration(reg.registrationId);
  assert.equal(frozen.status, RegistrationStatus.FROZEN);

  const result = service.completeRegistration({
    registrationId: reg.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'cred-frozen' }),
  });
  assert.equal(result.status, RegistrationStatus.REJECTED);
  assert.equal(result.reason, ErrorCodes.REGISTRATION_FROZEN);
  assert.equal(service.listCredentials('acct-1').length, 0);
});

test('应被阻止：账户会话被替换后，挂在旧会话上的登记不得完成', () => {
  const { service } = createService();
  const oldSession = service.login('acct-1');
  const reg = service.startRegistration({ accountSessionId: oldSession.sessionId, challenge: 'challenge-old-session' });

  const newSession = service.replaceAccountSession('acct-1');
  assert.equal(service.getAccountSession(oldSession.sessionId).status, 'replaced');
  assert.equal(service.getAccountSession(newSession.sessionId).status, 'active');

  const result = service.completeRegistration({
    registrationId: reg.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'cred-replaced' }),
  });
  assert.equal(result.status, RegistrationStatus.REJECTED);
  assert.equal(result.reason, ErrorCodes.SESSION_REPLACED);

  // 新会话上发起的登记不受影响，仍可成功。
  const reg2 = service.startRegistration({ accountSessionId: newSession.sessionId, challenge: 'challenge-new-session' });
  const ok = service.completeRegistration({
    registrationId: reg2.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'cred-new' }),
  });
  assert.equal(ok.status, RegistrationStatus.SUCCEEDED);
});

test('应被阻止：同一账户重复绑定同一凭据编号', () => {
  const { service } = createService();
  const session = service.login('acct-1');
  const reg1 = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'c-dup-1' });
  service.completeRegistration({
    registrationId: reg1.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'shared-cred' }),
  });

  const reg2 = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'c-dup-2' });
  const result = service.completeRegistration({
    registrationId: reg2.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'shared-cred' }),
  });
  assert.equal(result.status, RegistrationStatus.REJECTED);
  assert.equal(result.reason, ErrorCodes.CREDENTIAL_ALREADY_BOUND);
  assert.equal(service.listCredentials('acct-1').length, 1);
});

test('幂等：重复回调回放原有成功结果，即使第二次携带不同凭据', () => {
  const { service } = createService();
  const session = service.login('acct-1');
  const reg = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'c-replay-ok' });
  const first = makeVerifiedCredential({ credentialId: 'cred-first', publicKey: 'pk-first' });

  const r1 = service.completeRegistration({ registrationId: reg.registrationId, credential: first });
  assert.equal(r1.status, RegistrationStatus.SUCCEEDED);
  assert.equal(r1.replay, false);

  const different = makeVerifiedCredential({ credentialId: 'cred-other', publicKey: 'pk-other' });
  const r2 = service.completeRegistration({ registrationId: reg.registrationId, credential: different });
  assert.equal(r2.status, RegistrationStatus.SUCCEEDED);
  assert.equal(r2.replay, true);
  assert.equal(r2.credential.credentialId, 'cred-first');
  assert.equal(r2.registration.outcome.credentialId, 'cred-first');

  // 第二次携带的凭据从未被绑定。
  assert.equal(service.listCredentials('acct-1').length, 1);
  assert.equal(service.listCredentials('acct-1')[0].credentialId, 'cred-first');
});

test('幂等：被拒绝后的重复回调回放同一拒绝原因', () => {
  const { service } = createService();
  const session = service.login('acct-1');
  const reg = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'c-replay-reject' });

  const r1 = service.completeRegistration({ registrationId: reg.registrationId, credential: { not: 'trusted' } });
  assert.equal(r1.status, RegistrationStatus.REJECTED);
  assert.equal(r1.replay, false);

  const r2 = service.completeRegistration({
    registrationId: reg.registrationId,
    credential: makeVerifiedCredential({ credentialId: 'too-late' }),
  });
  assert.equal(r2.status, RegistrationStatus.REJECTED);
  assert.equal(r2.replay, true);
  assert.equal(r2.reason, ErrorCodes.NOT_TRUSTED_CREDENTIAL);
  assert.equal(service.listCredentials('acct-1').length, 0);
});

test('验签组件返回失败结构时，登记落为 rejected 且不产生绑定', () => {
  let tick = 0;
  const clock = () => `2026-11-19T00:00:${String(tick++).padStart(2, '0')}.000Z`;
  const verifier = new LocalVerifier(
    new Map([['c-verifier-fail', { ok: false, reason: 'attestation_invalid' }]]),
  );
  const service = new PasskeyRegistrationService({ verifier, clock });
  const session = service.login('acct-1');
  const reg = service.startRegistration({ accountSessionId: session.sessionId, challenge: 'c-verifier-fail' });

  const verdict = verifier.verify({ challenge: 'c-verifier-fail' });
  assert.equal(verdict.ok, false);

  const result = service.completeRegistration({ registrationId: reg.registrationId, credential: verdict });
  assert.equal(result.status, RegistrationStatus.REJECTED);
  assert.equal(result.reason, ErrorCodes.NOT_TRUSTED_CREDENTIAL);
  assert.equal(service.listCredentials('acct-1').length, 0);
});

test('TrustedCredential 实例被冻结，无法在流转中被篡改', () => {
  const credential = makeVerifiedCredential({ credentialId: 'cred-immutable' });
  assert.ok(credential instanceof TrustedCredential);
  assert.throws(() => {
    credential.credentialId = 'tampered';
  }, TypeError);
});

test('未知登记会话与未知账户会话按契约抛类型化错误', () => {
  const { service } = createService();
  assert.throws(
    () => service.completeRegistration({ registrationId: 'no-such-reg', credential: makeVerifiedCredential({ credentialId: 'x' }) }),
    (err) => err instanceof RegistrationError && err.code === ErrorCodes.UNKNOWN_REGISTRATION,
  );
  assert.throws(
    () => service.startRegistration({ accountSessionId: 'no-such-session', challenge: 'c-x' }),
    (err) => err instanceof RegistrationError && err.code === ErrorCodes.UNKNOWN_ACCOUNT_SESSION,
  );
});
