import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  ImportCoordinator,
  RegistrationState,
  ApplicationStatus,
  RejectReason,
} from '../src/import-coordinator.mjs';

const FP = 'fp-sha256-aaaa';

function makeCoordinator() {
  const c = new ImportCoordinator();
  c.confirmMaterial({ fingerprint: FP, confirmedBy: 'hsm-east-1', confirmedAt: 1 });
  return c;
}

function app(overrides = {}) {
  return {
    applicationId: 'app-1',
    businessId: 'biz-pay',
    fingerprint: FP,
    purpose: 'signing',
    issuedAt: 10,
    ...overrides,
  };
}

function registration(overrides = {}) {
  return {
    registrationId: 'reg-1',
    deviceId: 'device-A',
    fingerprint: FP,
    purpose: 'signing',
    createdAt: 100,
    ...overrides,
  };
}

function receipt(overrides = {}) {
  return {
    receiptId: 'rcpt-1',
    registrationId: 'reg-1',
    deviceId: 'device-A',
    fingerprint: FP,
    importProven: true,
    purposeBindingProven: true,
    issuedAt: 200,
    ...overrides,
  };
}

test('可接受路径：申请受理 → 设备登记 → 完整回执启用登记', () => {
  const c = makeCoordinator();

  const decision = c.submitApplication(app());
  assert.equal(decision.status, ApplicationStatus.ACCEPTED);
  assert.equal(c.getMaterial(FP).lockedPurpose, 'signing');

  const reg = c.registerDevice(registration());
  assert.equal(reg.ok, true);
  assert.equal(reg.registration.state, RegistrationState.PENDING);

  const res = c.submitReceipt(receipt());
  assert.equal(res.ok, true);
  assert.equal(res.state, RegistrationState.ACTIVE);

  const stored = c.getRegistration('reg-1');
  assert.equal(stored.state, RegistrationState.ACTIVE);
  assert.equal(stored.enabledAt, 200);
  assert.deepEqual(stored.receipts, ['rcpt-1']);
});

test('可接受路径：同材料同用途可登记到多台设备并各自启用', () => {
  const c = makeCoordinator();
  c.submitApplication(app());

  assert.equal(c.registerDevice(registration()).ok, true);
  assert.equal(
    c.registerDevice(registration({ registrationId: 'reg-2', deviceId: 'device-B' })).ok,
    true,
  );

  assert.equal(c.submitReceipt(receipt()).state, RegistrationState.ACTIVE);
  assert.equal(
    c.submitReceipt(
      receipt({ receiptId: 'rcpt-2', registrationId: 'reg-2', deviceId: 'device-B' }),
    ).state,
    RegistrationState.ACTIVE,
  );

  assert.equal(c.getRegistration('reg-1').state, RegistrationState.ACTIVE);
  assert.equal(c.getRegistration('reg-2').state, RegistrationState.ACTIVE);
});

test('应被拒绝：同一材料的不兼容用途在申请与登记两个环节都被阻止', () => {
  const c = makeCoordinator();
  assert.equal(c.submitApplication(app()).status, ApplicationStatus.ACCEPTED);

  // 登记已通过不代表同材料可再登记为不兼容用途：申请环节拒绝
  const late = c.submitApplication(app({ applicationId: 'app-2', purpose: 'backup' }));
  assert.equal(late.status, ApplicationStatus.REJECTED);
  assert.equal(late.reason, RejectReason.PURPOSE_MISMATCH);

  // 登记环节同样拒绝不兼容用途
  const reg = c.registerDevice(registration({ purpose: 'backup' }));
  assert.equal(reg.ok, false);
  assert.equal(reg.reason, RejectReason.PURPOSE_MISMATCH);
  assert.equal(c.getRegistration('reg-1'), undefined);

  // 已受理用途的登记不受影响
  assert.equal(c.registerDevice(registration()).ok, true);
});

test('竞争裁决：同时到达的同材料冲突申请确定性一胜一负，与提交顺序无关', () => {
  const run = (order) => {
    const c = makeCoordinator();
    const a = app({ applicationId: 'app-x', purpose: 'signing', issuedAt: 10 });
    const b = app({ applicationId: 'app-y', purpose: 'backup', issuedAt: 10 });
    const [ra, rb] = order === 'ab' ? [a, b] : [b, a];
    const results = c.submitApplicationBatch([ra, rb]);
    return { c, byId: new Map(results.map((r) => [r.applicationId, r])) };
  };

  for (const order of ['ab', 'ba']) {
    const { c, byId } = run(order);
    // issuedAt 并列时 applicationId 字典序小者（app-x / signing）胜出
    assert.equal(byId.get('app-x').status, ApplicationStatus.ACCEPTED);
    assert.equal(byId.get('app-y').status, ApplicationStatus.REJECTED);
    assert.equal(byId.get('app-y').reason, RejectReason.PURPOSE_COMPETITION_LOST);
    assert.equal(c.getMaterial(FP).lockedPurpose, 'signing');
    // 败方用途的后续登记同样被阻止
    const reg = c.registerDevice(registration({ purpose: 'backup' }));
    assert.equal(reg.ok, false);
    assert.equal(reg.reason, RejectReason.PURPOSE_MISMATCH);
  }
});

test('边界交错：仅证明导入成功的回执使登记停留待核验，补交用途绑定证明后才启用', () => {
  const c = makeCoordinator();
  c.submitApplication(app());
  c.registerDevice(registration());

  // 回执只证明导入成功，未证明绑定用途 → 待核验，不得启用
  const partial = c.submitReceipt(receipt({ purposeBindingProven: false }));
  assert.equal(partial.ok, true);
  assert.equal(partial.state, RegistrationState.PENDING_VERIFICATION);
  assert.equal(c.getRegistration('reg-1').enabledAt, null);

  // 补交证明用途绑定的回执 → 启用
  const full = c.submitReceipt(
    receipt({ receiptId: 'rcpt-2', purposeBindingProven: true, issuedAt: 300 }),
  );
  assert.equal(full.state, RegistrationState.ACTIVE);
  assert.equal(c.getRegistration('reg-1').enabledAt, 300);

  // 已启用后到达的弱证明回执不得降级登记
  const weak = c.submitReceipt(
    receipt({ receiptId: 'rcpt-3', purposeBindingProven: false, issuedAt: 400 }),
  );
  assert.equal(weak.state, RegistrationState.ACTIVE);
});

test('重试延续原登记：同参数重报不重置状态，改参数的重试被拒绝', () => {
  const c = makeCoordinator();
  c.submitApplication(app());
  c.registerDevice(registration());
  c.submitReceipt(receipt({ purposeBindingProven: false }));
  assert.equal(c.getRegistration('reg-1').state, RegistrationState.PENDING_VERIFICATION);

  // 重试：同 registrationId 同参数 → 延续原登记，状态不回退
  const retry = c.registerDevice(registration());
  assert.equal(retry.ok, true);
  assert.equal(retry.continued, true);
  assert.equal(retry.registration.state, RegistrationState.PENDING_VERIFICATION);
  assert.deepEqual(retry.registration.receipts, ['rcpt-1']);

  // 同 registrationId 但换成另一台设备 → 非法重试，拒绝
  const hijack = c.registerDevice(registration({ deviceId: 'device-B' }));
  assert.equal(hijack.ok, false);
  assert.equal(hijack.reason, RejectReason.RETRY_MISMATCH);
  assert.equal(c.getRegistration('reg-1').deviceId, 'device-A');
});

test('旧回执不能给新设备启用：过期回执与串用回执均被拒绝', () => {
  const c = makeCoordinator();
  c.submitApplication(app());

  // 旧设备 reg-1 已完成启用
  c.registerDevice(registration());
  assert.equal(c.submitReceipt(receipt()).state, RegistrationState.ACTIVE);

  // 同材料同用途的新设备登记（创建时间更晚）
  assert.equal(
    c.registerDevice(
      registration({ registrationId: 'reg-2', deviceId: 'device-B', createdAt: 500 }),
    ).ok,
    true,
  );

  // 把 reg-1 时代的旧回执（issuedAt=200 < createdAt=500）指向新登记 → 过期，拒绝
  const stale = c.submitReceipt(
    receipt({ receiptId: 'rcpt-old', registrationId: 'reg-2', deviceId: 'device-B', issuedAt: 200 }),
  );
  assert.equal(stale.ok, false);
  assert.equal(stale.reason, RejectReason.STALE_RECEIPT);
  assert.equal(c.getRegistration('reg-2').state, RegistrationState.PENDING);

  // 把旧设备的回执直接串用到新设备 → 设备不匹配，拒绝
  const cross = c.submitReceipt(
    receipt({ receiptId: 'rcpt-cross', registrationId: 'reg-2', deviceId: 'device-A', issuedAt: 600 }),
  );
  assert.equal(cross.ok, false);
  assert.equal(cross.reason, RejectReason.RECEIPT_MISMATCH);
  assert.equal(c.getRegistration('reg-2').state, RegistrationState.PENDING);

  // 重复提交已应用过的旧回执 → 幂等，不影响新登记
  const dup = c.submitReceipt(receipt());
  assert.equal(dup.duplicate, true);
  assert.equal(c.getRegistration('reg-2').state, RegistrationState.PENDING);

  // 新设备自己的合规回执仍可正常启用
  const own = c.submitReceipt(
    receipt({ receiptId: 'rcpt-new', registrationId: 'reg-2', deviceId: 'device-B', issuedAt: 600 }),
  );
  assert.equal(own.ok, true);
  assert.equal(own.state, RegistrationState.ACTIVE);
});

test('前置约束：未确认指纹或无已受理申请时登记被拒绝', () => {
  const c = new ImportCoordinator();

  const noMaterial = c.registerDevice(registration());
  assert.equal(noMaterial.ok, false);
  assert.equal(noMaterial.reason, RejectReason.UNKNOWN_MATERIAL);

  c.confirmMaterial({ fingerprint: FP });
  const noApp = c.registerDevice(registration());
  assert.equal(noApp.ok, false);
  assert.equal(noApp.reason, RejectReason.NO_ACCEPTED_APPLICATION);
});
