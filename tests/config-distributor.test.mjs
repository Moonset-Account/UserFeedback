import { test } from 'node:test';
import assert from 'node:assert/strict';

import { ConfigDistributor, DistributorError, DENY_REASONS } from '../src/config-distributor.mjs';

function createHarness(defaultTtlMs = 1000) {
  let now = 1000;
  const distributor = new ConfigDistributor({ now: () => now, defaultTtlMs });
  return {
    distributor,
    get time() {
      return now;
    },
    setTime(value) {
      now = value;
    },
    advance(ms) {
      now += ms;
    },
  };
}

function barrier(onPhase) {
  return async (phase, context) => {
    if (onPhase) onPhase(phase, context);
  };
}

function setupV1(ttlMs = 1000) {
  const harness = createHarness(ttlMs);
  const { distributor } = harness;
  distributor.publishVersion('v1', {
    endpoint: 'https://svc.example/v1',
    apiKey: 'topsecret-v1',
    region: 'cn',
  });
  distributor.registerInstance({
    instanceId: 'inst-1',
    generation: 0,
    approvedFields: ['endpoint', 'apiKey', 'region'],
  });
  return harness;
}

test('可接受结果：完整授权读取返回完整版本并写入 granted 审计', async () => {
  const { distributor } = setupV1();
  const lease = await distributor.issueLease({ instanceId: 'inst-1', generation: 0 });

  const result = await distributor.readConfig({
    leaseId: lease.leaseId,
    fields: ['endpoint', 'apiKey'],
  });

  assert.equal(result.granted, true);
  assert.equal(result.version, 'v1');
  assert.deepEqual(result.config, {
    endpoint: 'https://svc.example/v1',
    apiKey: 'topsecret-v1',
  });

  const audit = distributor.getAuditLog();
  assert.equal(audit.length, 1);
  assert.deepEqual(audit[0], { version: 'v1', decision: 'granted' });
});

test('可接受结果：部分字段授权时只得到所请求的批准子集', async () => {
  const harness = createHarness();
  const { distributor } = harness;
  distributor.publishVersion('v1', { a: 'secret-a', b: 'secret-b', c: 'secret-c' });
  distributor.registerInstance({
    instanceId: 'inst-2',
    generation: 0,
    approvedFields: ['a', 'c'],
  });
  const lease = await distributor.issueLease({ instanceId: 'inst-2', generation: 0 });

  const result = await distributor.readConfig({ leaseId: lease.leaseId, fields: ['a'] });

  assert.equal(result.granted, true);
  assert.deepEqual(result.config, { a: 'secret-a' });
  assert.deepEqual(Object.keys(result.config), ['a']);
});

test('应被阻止：请求未批准字段时整次拒绝且不发放任何字段', async () => {
  const { distributor } = setupV1();
  distributor.registerInstance({
    instanceId: 'inst-3',
    generation: 0,
    approvedFields: ['endpoint'],
  });
  const lease = await distributor.issueLease({ instanceId: 'inst-3', generation: 0 });

  const result = await distributor.readConfig({
    leaseId: lease.leaseId,
    fields: ['endpoint', 'apiKey'],
  });

  assert.equal(result.granted, false);
  assert.equal(result.reason, DENY_REASONS.FIELD_NOT_APPROVED);
  assert.equal(result.config, undefined);
  assert.deepEqual(distributor.getAuditLog().at(-1), { version: 'v1', decision: 'denied' });
});

test('应被阻止：紧急撤销后该版本全部停止发放（含已发出的租约）', async () => {
  const { distributor } = setupV1();
  const lease = await distributor.issueLease({ instanceId: 'inst-1', generation: 0 });

  distributor.revokeVersion('v1');
  const result = await distributor.readConfig({
    leaseId: lease.leaseId,
    fields: ['endpoint'],
  });

  assert.equal(result.granted, false);
  assert.equal(result.reason, DENY_REASONS.VERSION_REVOKED);
  assert.equal(distributor.isVersionRevoked('v1'), true);
  assert.deepEqual(distributor.getAuditLog().at(-1), { version: 'v1', decision: 'denied' });

  // 撤销后不再允许为该版本发放新租约（当前版本仍指向被撤销版本时）。
  await assert.rejects(
    () => distributor.issueLease({ instanceId: 'inst-1', generation: 0 }),
    (error) => error instanceof DistributorError && error.reason === DENY_REASONS.VERSION_REVOKED,
  );
});

test('应被阻止：实例重启代次推进后旧租约不可复用', async () => {
  const { distributor } = setupV1();
  const oldLease = await distributor.issueLease({ instanceId: 'inst-1', generation: 0 });

  distributor.registerInstance({
    instanceId: 'inst-1',
    generation: 1,
    approvedFields: ['endpoint', 'apiKey', 'region'],
  });

  const staleRead = await distributor.readConfig({
    leaseId: oldLease.leaseId,
    fields: ['endpoint'],
  });
  assert.equal(staleRead.granted, false);
  assert.equal(staleRead.reason, DENY_REASONS.GENERATION_MISMATCH);

  // 旧代次也不能再申请新租约；登记代次不允许回退。
  await assert.rejects(
    () => distributor.issueLease({ instanceId: 'inst-1', generation: 0 }),
    (error) => error.reason === DENY_REASONS.GENERATION_MISMATCH,
  );
  assert.throws(
    () =>
      distributor.registerInstance({
        instanceId: 'inst-1',
        generation: 1,
        approvedFields: ['endpoint'],
      }),
    (error) => error instanceof DistributorError,
  );

  // 新代次租约正常工作，且旧租约再次使用仍被拒绝。
  const newLease = await distributor.issueLease({ instanceId: 'inst-1', generation: 1 });
  const okRead = await distributor.readConfig({ leaseId: newLease.leaseId, fields: ['endpoint'] });
  assert.equal(okRead.granted, true);
  const retryOld = await distributor.readConfig({ leaseId: oldLease.leaseId, fields: ['endpoint'] });
  assert.equal(retryOld.granted, false);
  assert.equal(retryOld.reason, DENY_REASONS.GENERATION_MISMATCH);
});

test('应被阻止：未知租约被拒绝，审计以 null 版本记录', async () => {
  const { distributor } = setupV1();
  const result = await distributor.readConfig({ leaseId: 'no-such-lease', fields: ['endpoint'] });

  assert.equal(result.granted, false);
  assert.equal(result.reason, DENY_REASONS.UNKNOWN_LEASE);
  assert.equal(result.version, null);
  assert.deepEqual(distributor.getAuditLog().at(-1), { version: null, decision: 'denied' });
});

test('边界：租约在到期前可用，到期时刻与到期后被拒绝（半开窗口）', async () => {
  const harness = setupV1(100); // ttl=100，颁发时刻 1000，到期时刻 1100
  const { distributor } = harness;
  const lease = await distributor.issueLease({ instanceId: 'inst-1', generation: 0 });
  assert.equal(lease.expiresAt, 1100);

  harness.setTime(1099);
  const before = await distributor.readConfig({ leaseId: lease.leaseId, fields: ['endpoint'] });
  assert.equal(before.granted, true);

  harness.setTime(1100); // now === expiresAt 即过期
  const onEdge = await distributor.readConfig({ leaseId: lease.leaseId, fields: ['endpoint'] });
  assert.equal(onEdge.granted, false);
  assert.equal(onEdge.reason, DENY_REASONS.LEASE_EXPIRED);

  harness.setTime(1500);
  const after = await distributor.readConfig({ leaseId: lease.leaseId, fields: ['endpoint'] });
  assert.equal(after.granted, false);
  assert.equal(after.reason, DENY_REASONS.LEASE_EXPIRED);
});

test('交错：读取等待期间发生普通版本切换，返回值仍来自一个完整的旧版本', async () => {
  const { distributor } = setupV1();
  const leaseV1 = await distributor.issueLease({ instanceId: 'inst-1', generation: 0 });

  const pendingRead = distributor.readConfig(
    { leaseId: leaseV1.leaseId, fields: ['endpoint', 'apiKey'] },
    barrier((phase) => {
      if (phase === 'lease-validated') {
        distributor.publishVersion('v2', {
          endpoint: 'https://svc.example/v2',
          apiKey: 'topsecret-v2',
          region: 'eu',
        });
      }
    }),
  );
  const result = await pendingRead;

  assert.equal(result.granted, true);
  assert.equal(result.version, 'v1');
  assert.deepEqual(result.config, {
    endpoint: 'https://svc.example/v1',
    apiKey: 'topsecret-v1',
  });

  // 普通更新保留旧实例有效读取窗口：旧租约再次读取仍为 v1。
  const again = await distributor.readConfig({ leaseId: leaseV1.leaseId, fields: ['region'] });
  assert.equal(again.granted, true);
  assert.equal(again.version, 'v1');
  assert.equal(again.config.region, 'cn');

  // 新租约钉住新版本，同样完整来自 v2，不存在跨版本拼接。
  const leaseV2 = await distributor.issueLease({ instanceId: 'inst-1', generation: 0 });
  assert.equal(leaseV2.version, 'v2');
  const readV2 = await distributor.readConfig({ leaseId: leaseV2.leaseId, fields: ['endpoint', 'region'] });
  assert.equal(readV2.granted, true);
  assert.deepEqual(readV2.config, { endpoint: 'https://svc.example/v2', region: 'eu' });
});

test('交错：读取等待期间发生紧急撤销，快照后复检拒绝发放', async () => {
  const { distributor } = setupV1();
  const lease = await distributor.issueLease({ instanceId: 'inst-1', generation: 0 });

  const result = await distributor.readConfig(
    { leaseId: lease.leaseId, fields: ['endpoint', 'apiKey'] },
    barrier((phase) => {
      if (phase === 'snapshot-captured') {
        distributor.revokeVersion('v1');
      }
    }),
  );

  assert.equal(result.granted, false);
  assert.equal(result.version, 'v1');
  assert.equal(result.reason, DENY_REASONS.VERSION_REVOKED);
  assert.deepEqual(distributor.getAuditLog().at(-1), { version: 'v1', decision: 'denied' });
});

test('交错：颁发租约等待期间发布新版本，租约钉住捕获时的完整版本', async () => {
  const { distributor } = setupV1();

  const leasePromise = distributor.issueLease(
    { instanceId: 'inst-1', generation: 0 },
    barrier((phase) => {
      if (phase === 'version-captured') {
        distributor.publishVersion('v2', {
          endpoint: 'https://svc.example/v2',
          apiKey: 'topsecret-v2',
          region: 'eu',
        });
      }
    }),
  );
  const lease = await leasePromise;
  assert.equal(lease.version, 'v1');

  const read = await distributor.readConfig({ leaseId: lease.leaseId, fields: ['apiKey'] });
  assert.equal(read.granted, true);
  assert.equal(read.version, 'v1');
  assert.equal(read.config.apiKey, 'topsecret-v1');
});

test('交错：读取等待期间实例重启，等待后按代次不匹配拒绝', async () => {
  const { distributor } = setupV1();
  const lease = await distributor.issueLease({ instanceId: 'inst-1', generation: 0 });

  const result = await distributor.readConfig(
    { leaseId: lease.leaseId, fields: ['endpoint'] },
    barrier((phase) => {
      if (phase === 'lease-validated') {
        distributor.registerInstance({
          instanceId: 'inst-1',
          generation: 1,
          approvedFields: ['endpoint', 'apiKey', 'region'],
        });
      }
    }),
  );

  assert.equal(result.granted, false);
  assert.equal(result.reason, DENY_REASONS.GENERATION_MISMATCH);
});

test('审计卫生：审计条目仅含 version 与 decision，且不含任何机密或标识信息', async () => {
  const { distributor } = setupV1();
  const lease = await distributor.issueLease({ instanceId: 'inst-1', generation: 0 });
  await distributor.readConfig({ leaseId: lease.leaseId, fields: ['apiKey'] });
  distributor.revokeVersion('v1');
  await distributor.readConfig({ leaseId: lease.leaseId, fields: ['apiKey'] });
  await distributor.readConfig({ leaseId: 'ghost', fields: ['apiKey'] });

  const audit = distributor.getAuditLog();
  assert.equal(audit.length, 3);
  for (const entry of audit) {
    assert.deepEqual(Object.keys(entry).sort(), ['decision', 'version']);
    assert.ok(Object.isFrozen(entry));
    assert.match(entry.decision, /^(granted|denied)$/);
  }
  assert.deepEqual(audit.map((e) => e.decision), ['granted', 'denied', 'denied']);
  assert.equal(audit.at(-1).version, null);

  const serialized = JSON.stringify(audit);
  for (const leak of ['topsecret', 'inst-1', lease.leaseId, 'apiKey', 'endpoint']) {
    assert.ok(!serialized.includes(leak), `audit log must not contain ${leak}`);
  }

  // 外部修改审计副本不能影响内部历史。
  audit.length = 0;
  assert.equal(distributor.getAuditLog().length, 3);
});

test('版本不可变：重复发布被拒绝，调用方事后改动输入不影响已发布快照', async () => {
  const { distributor } = setupV1();
  assert.throws(
    () => distributor.publishVersion('v1', { endpoint: 'other' }),
    (error) => error instanceof DistributorError && error.reason === 'version-exists',
  );

  const mutable = { endpoint: 'https://svc.example/v2', apiKey: 'topsecret-v2' };
  distributor.publishVersion('v2', mutable);
  mutable.endpoint = 'mutated-by-caller';
  mutable.apiKey = 'mutated';

  const lease = await distributor.issueLease({ instanceId: 'inst-1', generation: 0 });
  const read = await distributor.readConfig({ leaseId: lease.leaseId, fields: ['endpoint', 'apiKey'] });
  assert.equal(read.version, 'v2');
  assert.equal(read.config.endpoint, 'https://svc.example/v2');
  assert.equal(read.config.apiKey, 'topsecret-v2');
  assert.ok(Object.isFrozen(read.config));
});

test('参数校验：空字段列表、重复字段与非法代次被拒绝', async () => {
  const { distributor } = setupV1();
  const lease = await distributor.issueLease({ instanceId: 'inst-1', generation: 0 });

  await assert.rejects(
    () => distributor.readConfig({ leaseId: lease.leaseId, fields: [] }),
    TypeError,
  );
  await assert.rejects(
    () => distributor.readConfig({ leaseId: lease.leaseId, fields: ['endpoint', 'endpoint'] }),
    TypeError,
  );
  assert.throws(
    () => distributor.registerInstance({ instanceId: 'x', generation: -1, approvedFields: [] }),
    TypeError,
  );
});
