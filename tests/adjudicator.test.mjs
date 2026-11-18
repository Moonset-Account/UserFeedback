import { describe, test } from 'node:test';
import assert from 'node:assert/strict';
import {
  createAdjudicationWorld,
  LogicalClock,
  REASONS
} from '../src/index.mjs';

const IDENTITY_A = 'spiffe://cluster.local/ns/payments/sa/billing';
const IDENTITY_B = 'spiffe://cluster.local/ns/ledger/sa/reconciler';
const DIGEST_A = 'sha256:aaaa1111';
const DIGEST_B = 'sha256:bbbb2222';

/** 可手动拨时间的时钟：实现与 LogicalClock 相同的 next()/wallMs()/now() 接口。 */
class MutableClock {
  #seq = 0;
  #currentMs;
  constructor(start = '2026-11-19T00:00:00.000Z') {
    this.#currentMs = Date.parse(start);
  }
  next() {
    this.#seq += 1;
    return this.#seq;
  }
  get sequence() {
    return this.#seq;
  }
  wallMs() {
    return this.#currentMs;
  }
  now() {
    return new Date(this.#currentMs).toISOString();
  }
  advance(ms) {
    this.#currentMs += ms;
  }
}

function makeClock() {
  return new LogicalClock('2026-11-19T00:00:00.000Z');
}

/** 标准夹具：身份登记 A + 实例 inst-1 的已验证测量 + 租约 v1。 */
function seedWorld(clock = makeClock()) {
  const world = createAdjudicationWorld({ clock });
  world.identities.put({
    serviceIdentity: IDENTITY_A,
    imageDigest: DIGEST_A,
    allowedAudiences: ['billing-api', 'ledger-api'],
    permissions: ['billing:read', 'billing:charge']
  });
  world.measurements.put({
    measurementId: 'meas-1',
    jobId: 'job-77',
    instanceId: 'inst-1',
    nodeId: 'node-A',
    imageDigest: DIGEST_A,
    verified: true
  });
  world.leases.put({
    leaseId: 'lease-1',
    version: 1,
    jobId: 'job-77',
    instanceId: 'inst-1',
    nodeId: 'node-A'
  });
  return world;
}

function baseRequest(overrides = {}) {
  return {
    requestId: 'req-1',
    jobId: 'job-77',
    instanceId: 'inst-1',
    nodeId: 'node-A',
    measurementId: 'meas-1',
    leaseId: 'lease-1',
    serviceIdentity: IDENTITY_A,
    audience: 'billing-api',
    permissions: ['billing:read'],
    ...overrides
  };
}

function assertAllow(decision, stage) {
  assert.equal(decision.verdict, 'ALLOW', `期望 ALLOW，实际: ${JSON.stringify(decision.reasons)}`);
  assert.ok(decision.decisionId.startsWith('dec-'));
  assert.deepEqual(decision.reasons, []);
  if (stage) assert.equal(decision.stage, stage);
  return decision;
}

function assertDeny(decision, code, stage) {
  assert.equal(decision.verdict, 'DENY');
  assert.equal(decision.reasons[0].code, code);
  assert.equal(decision.reasons[0].message, REASONS[code].message);
  assert.ok(decision.decisionId.startsWith('dec-'));
  assert.ok(decision.evidence !== undefined && decision.evidence !== null, 'DENY 必须携带可追溯证据');
  if (stage) assert.equal(decision.stage, stage);
  return decision;
}

function approveAndIssue(world, request) {
  const approval = assertAllow(
    world.adjudicator.requestApproval(request),
    'APPROVAL'
  );
  const issuance = assertAllow(world.adjudicator.issue({
    approvalId: approval.approvalId,
    requestId: request.requestId,
    leaseVersion: request.leaseId ? world.leases.get(request.leaseId).version : undefined,
    audience: request.audience
  }), 'ISSUANCE');
  return { approval, issuance };
}

// ---------------------------------------------------------------------------
describe('可接受链路与可追溯性', () => {
  test('完整审批+签发通过，凭证绑定租约版本与受众，决策全程可追溯', () => {
    const world = seedWorld();

    const approval = assertAllow(
      world.adjudicator.requestApproval(baseRequest()),
      'APPROVAL'
    );
    assert.equal(approval.approvalId, 'apv-1');
    assert.deepEqual(approval.basis.bindings, { leaseVersion: 1, audience: 'billing-api' });
    assert.equal(approval.basis.measurement.measurementId, 'meas-1');
    assert.equal(approval.basis.identity.imageDigest, DIGEST_A);

    const issuance = assertAllow(world.adjudicator.issue({
      approvalId: approval.approvalId,
      requestId: 'req-1',
      leaseVersion: 1,
      audience: 'billing-api'
    }), 'ISSUANCE');

    // 凭证内嵌全部绑定事实
    assert.equal(issuance.credential.audience, 'billing-api');
    assert.deepEqual(issuance.credential.lease, { leaseId: 'lease-1', version: 1 });
    assert.deepEqual(issuance.credential.subject, {
      jobId: 'job-77', instanceId: 'inst-1', nodeId: 'node-A'
    });
    assert.equal(issuance.credential.measurementId, 'meas-1');
    assert.equal(issuance.credential.imageDigest, DIGEST_A);
    assert.equal(issuance.credential.approvalId, approval.approvalId);

    // 审批已被一次性消费
    assert.equal(world.approvals.get(approval.approvalId).consumed, true);
    assert.equal(world.approvals.get(approval.approvalId).credentialId, issuance.credentialId);

    // 决策日志：按 decisionId / requestId 均可追溯
    const log = world.adjudicator.getDecisionLog();
    assert.equal(log.length, 2);
    assert.ok(log.every((d) => d.evaluatedAt && d.decisionId));
    assert.equal(world.adjudicator.getDecision(approval.decisionId).verdict, 'ALLOW');
    const byReq = world.adjudicator.getDecisionsForRequest('req-1');
    assert.deepEqual(byReq.map((d) => d.stage).sort(), ['APPROVAL', 'ISSUANCE']);
  });
});

// ---------------------------------------------------------------------------
describe('启动测量仅可用于原节点上的原作业实例', () => {
  test('调度重试的新实例复用旧测量被阻止，必须提供自己的测量', () => {
    const world = seedWorld();
    const decision = world.adjudicator.requestApproval(
      baseRequest({ requestId: 'req-retry', instanceId: 'inst-2' })
    );
    const deny = assertDeny(decision, 'MEASUREMENT_INSTANCE_MISMATCH', 'APPROVAL');
    assert.equal(deny.evidence.measurement.instanceId, 'inst-1');
    assert.equal(deny.evidence.requested.instanceId, 'inst-2');
  });

  test('测量搬到另一节点使用被阻止', () => {
    const world = seedWorld();
    const decision = world.adjudicator.requestApproval(
      baseRequest({ requestId: 'req-node', nodeId: 'node-B' })
    );
    assertDeny(decision, 'MEASUREMENT_NODE_MISMATCH', 'APPROVAL');
  });

  test('测量跨作业使用被阻止', () => {
    const world = seedWorld();
    const decision = world.adjudicator.requestApproval(
      baseRequest({ requestId: 'req-job', jobId: 'job-99' })
    );
    assertDeny(decision, 'MEASUREMENT_JOB_MISMATCH', 'APPROVAL');
  });

  test('未通过验证的测量不能用于申请', () => {
    const world = seedWorld();
    world.measurements.put({
      measurementId: 'meas-bad',
      jobId: 'job-77', instanceId: 'inst-1', nodeId: 'node-A',
      imageDigest: DIGEST_A, verified: false
    });
    const decision = world.adjudicator.requestApproval(
      baseRequest({ measurementId: 'meas-bad' })
    );
    assertDeny(decision, 'MEASUREMENT_NOT_VERIFIED', 'APPROVAL');
  });

  test('测量登记缺失被拒绝', () => {
    const world = seedWorld();
    const decision = world.adjudicator.requestApproval(
      baseRequest({ measurementId: 'meas-nope' })
    );
    assertDeny(decision, 'MEASUREMENT_NOT_FOUND', 'APPROVAL');
  });

  test('边界：重试新实例提交自己的新测量并取得自己的租约，可以通过', () => {
    const world = seedWorld();
    world.measurements.put({
      measurementId: 'meas-2',
      jobId: 'job-77', instanceId: 'inst-2', nodeId: 'node-A',
      imageDigest: DIGEST_A, verified: true
    });
    world.leases.put({
      leaseId: 'lease-2', version: 2, replacesLeaseId: 'lease-1',
      jobId: 'job-77', instanceId: 'inst-2', nodeId: 'node-A'
    });

    const { issuance } = approveAndIssue(world, baseRequest({
      requestId: 'req-retry-ok',
      instanceId: 'inst-2',
      measurementId: 'meas-2',
      leaseId: 'lease-2'
    }));
    assert.deepEqual(issuance.credential.subject.instanceId, 'inst-2');
    assert.equal(issuance.credential.measurementId, 'meas-2');
    assert.equal(issuance.credential.lease.version, 2);
  });
});

// ---------------------------------------------------------------------------
describe('租约版本绑定与租约替换', () => {
  test('租约替换后旧审批不可消费，按新版本重新审批可以通过', () => {
    const world = seedWorld();
    const approval = assertAllow(
      world.adjudicator.requestApproval(baseRequest({ requestId: 'req-old' })),
      'APPROVAL'
    );

    // 调度器重试续约：lease-2 替换 lease-1
    world.leases.put({
      leaseId: 'lease-2', version: 2, replacesLeaseId: 'lease-1',
      jobId: 'job-77', instanceId: 'inst-1', nodeId: 'node-A'
    });
    assert.equal(world.leases.get('lease-1').status, 'REPLACED');
    assert.equal(world.leases.get('lease-2').status, 'ACTIVE');

    const staleIssue = world.adjudicator.issue({
      approvalId: approval.approvalId,
      requestId: 'req-old',
      leaseVersion: 1,
      audience: 'billing-api'
    });
    assertDeny(staleIssue, 'LEASE_NOT_ACTIVE', 'ISSUANCE');

    // 旧审批未被消费，新租约走新审批
    assert.equal(world.approvals.get(approval.approvalId).consumed, false);
    const reapproval = assertAllow(
      world.adjudicator.requestApproval(
        baseRequest({ requestId: 'req-new', leaseId: 'lease-2' })
      ),
      'APPROVAL'
    );
    assert.equal(reapproval.basis.bindings.leaseVersion, 2);
  });

  test('消费时重申的租约版本与审批绑定不一致被阻止', () => {
    const world = seedWorld();
    const approval = assertAllow(
      world.adjudicator.requestApproval(baseRequest()),
      'APPROVAL'
    );
    const decision = world.adjudicator.issue({
      approvalId: approval.approvalId,
      requestId: 'req-1',
      leaseVersion: 99,
      audience: 'billing-api'
    });
    assertDeny(decision, 'APPROVAL_BINDING_MISMATCH', 'ISSUANCE');
  });

  test('租约未到生效窗口与已过期在审批阶段被拒绝', () => {
    const future = seedWorld(new MutableClock());
    future.leases.put({
      leaseId: 'lease-future', version: 1,
      jobId: 'job-77', instanceId: 'inst-1', nodeId: 'node-A',
      validFrom: '2026-11-19T00:05:00.000Z'
    });
    assertDeny(
      future.adjudicator.requestApproval(baseRequest({ leaseId: 'lease-future' })),
      'LEASE_NOT_YET_VALID',
      'APPROVAL'
    );

    const expired = seedWorld(new MutableClock());
    expired.leases.put({
      leaseId: 'lease-expired', version: 1,
      jobId: 'job-77', instanceId: 'inst-1', nodeId: 'node-A',
      validUntil: '2026-11-18T23:55:00.000Z'
    });
    assertDeny(
      expired.adjudicator.requestApproval(baseRequest({ leaseId: 'lease-expired' })),
      'LEASE_EXPIRED',
      'APPROVAL'
    );
  });

  test('交错边界：审批通过后租约到期，随后签发被阻断', () => {
    const world = seedWorld(new MutableClock());
    world.leases.put({
      leaseId: 'lease-window', version: 1,
      jobId: 'job-77', instanceId: 'inst-1', nodeId: 'node-A',
      validUntil: '2026-11-19T00:00:01.000Z'
    });
    const approval = assertAllow(
      world.adjudicator.requestApproval(baseRequest({ leaseId: 'lease-window' })),
      'APPROVAL'
    );
    world.clock.advance(5000);
    const issuance = world.adjudicator.issue({
      approvalId: approval.approvalId,
      requestId: 'req-1',
      leaseVersion: 1,
      audience: 'billing-api'
    });
    assertDeny(issuance, 'LEASE_EXPIRED', 'ISSUANCE');
  });

  test('租约不存在或范围不匹配被拒绝', () => {
    const world = seedWorld();
    assertDeny(
      world.adjudicator.requestApproval(baseRequest({ leaseId: 'lease-nope' })),
      'LEASE_NOT_FOUND',
      'APPROVAL'
    );
    world.leases.put({
      leaseId: 'lease-other', version: 1,
      jobId: 'job-77', instanceId: 'inst-2', nodeId: 'node-A'
    });
    assertDeny(
      world.adjudicator.requestApproval(baseRequest({ leaseId: 'lease-other' })),
      'LEASE_SCOPE_MISMATCH',
      'APPROVAL'
    );
  });
});

// ---------------------------------------------------------------------------
describe('发布许可撤销阻断随后续发', () => {
  test('审批前许可已撤销：申请直接被阻止', () => {
    const world = seedWorld();
    world.revocations.publish({
      scope: 'PERMISSION',
      serviceIdentity: IDENTITY_A,
      permission: 'billing:read',
      reason: 'incident-42'
    });
    const decision = world.adjudicator.requestApproval(baseRequest());
    const deny = assertDeny(decision, 'PERMISSION_REVOKED', 'APPROVAL');
    assert.equal(deny.evidence.revocation.reason, 'incident-42');
  });

  test('交错：先审批通过，之后撤销许可，消费签发仍被阻断', () => {
    const world = seedWorld();
    const approval = assertAllow(
      world.adjudicator.requestApproval(baseRequest({ requestId: 'req-then-revoke' })),
      'APPROVAL'
    );
    world.revocations.publish({
      scope: 'PERMISSION',
      serviceIdentity: IDENTITY_A,
      permission: 'billing:read'
    });
    const issuance = world.adjudicator.issue({
      approvalId: approval.approvalId,
      requestId: 'req-then-revoke',
      leaseVersion: 1,
      audience: 'billing-api'
    });
    assertDeny(issuance, 'PERMISSION_REVOKED', 'ISSUANCE');
    assert.equal(world.approvals.get(approval.approvalId).consumed, false);
  });

  test('受众级撤销只阻断该受众，另一受众仍可签发', () => {
    const world = seedWorld();
    world.revocations.publish({
      scope: 'AUDIENCE',
      serviceIdentity: IDENTITY_A,
      audience: 'billing-api'
    });
    assertDeny(
      world.adjudicator.requestApproval(baseRequest({ audience: 'billing-api' })),
      'AUDIENCE_REVOKED',
      'APPROVAL'
    );
    approveAndIssue(world, baseRequest({
      requestId: 'req-other-aud',
      audience: 'ledger-api'
    }));
  });

  test('身份级撤销阻断该身份的一切申请', () => {
    const world = seedWorld();
    world.revocations.publish({ scope: 'IDENTITY', serviceIdentity: IDENTITY_A });
    assertDeny(
      world.adjudicator.requestApproval(baseRequest()),
      'IDENTITY_REVOKED',
      'APPROVAL'
    );
  });

  test('边界：撤销的是未申请的许可，不影响本次裁决', () => {
    const world = seedWorld();
    world.revocations.publish({
      scope: 'PERMISSION',
      serviceIdentity: IDENTITY_A,
      permission: 'billing:charge'
    });
    approveAndIssue(world, baseRequest({ requestId: 'req-unrelated' }));
  });
});

// ---------------------------------------------------------------------------
describe('身份登记、镜像摘要与受众约束', () => {
  test('受众不在身份登记允许集合内被拒绝；消费时串用受众被绑定拦截', () => {
    const world = seedWorld();
    assertDeny(
      world.adjudicator.requestApproval(baseRequest({ audience: 'evil-api' })),
      'AUDIENCE_NOT_PERMITTED',
      'APPROVAL'
    );

    const approval = assertAllow(
      world.adjudicator.requestApproval(baseRequest()),
      'APPROVAL'
    );
    const swapped = world.adjudicator.issue({
      approvalId: approval.approvalId,
      requestId: 'req-1',
      leaseVersion: 1,
      audience: 'ledger-api'
    });
    assertDeny(swapped, 'APPROVAL_BINDING_MISMATCH', 'ISSUANCE');
  });

  test('镜像摘要与身份登记不一致被拒绝', () => {
    const world = seedWorld();
    world.measurements.put({
      measurementId: 'meas-other-image',
      jobId: 'job-77', instanceId: 'inst-1', nodeId: 'node-A',
      imageDigest: 'sha256:cccc3333', verified: true
    });
    assertDeny(
      world.adjudicator.requestApproval(
        baseRequest({ measurementId: 'meas-other-image' })
      ),
      'IMAGE_DIGEST_MISMATCH',
      'APPROVAL'
    );
  });

  test('服务身份未登记被拒绝', () => {
    const world = seedWorld();
    assertDeny(
      world.adjudicator.requestApproval(
        baseRequest({ serviceIdentity: 'spiffe://cluster.local/ns/ghost/sa/nobody' })
      ),
      'IDENTITY_NOT_REGISTERED',
      'APPROVAL'
    );
  });
});

// ---------------------------------------------------------------------------
describe('审批一次性消费与综合交错时间线', () => {
  test('审批只能消费一次，重复消费被阻止并追溯到已发凭证', () => {
    const world = seedWorld();
    const approval = assertAllow(
      world.adjudicator.requestApproval(baseRequest()),
      'APPROVAL'
    );
    const first = assertAllow(world.adjudicator.issue({
      approvalId: approval.approvalId,
      requestId: 'req-1',
      leaseVersion: 1,
      audience: 'billing-api'
    }), 'ISSUANCE');

    const second = world.adjudicator.issue({
      approvalId: approval.approvalId,
      requestId: 'req-1',
      leaseVersion: 1,
      audience: 'billing-api'
    });
    const deny = assertDeny(second, 'APPROVAL_ALREADY_CONSUMED', 'ISSUANCE');
    assert.equal(deny.evidence.credentialId, first.credentialId);
  });

  test('综合交错：重试→测量绑定拦截→租约替换→许可撤销→更换许可恢复签发', () => {
    const world = seedWorld();

    // 1. 原实例完整签发
    const first = approveAndIssue(world, baseRequest({ requestId: 'req-inst-1' }));
    assert.equal(first.issuance.credential.lease.version, 1);

    // 2. 调度重试：新实例试图复用旧测量，被拦截
    assertDeny(
      world.adjudicator.requestApproval(
        baseRequest({ requestId: 'req-retry-blocked', instanceId: 'inst-2' })
      ),
      'MEASUREMENT_INSTANCE_MISMATCH',
      'APPROVAL'
    );

    // 3. 新实例登记自己的测量；新租约替换旧租约
    world.measurements.put({
      measurementId: 'meas-2',
      jobId: 'job-77', instanceId: 'inst-2', nodeId: 'node-A',
      imageDigest: DIGEST_A, verified: true
    });
    world.leases.put({
      leaseId: 'lease-2', version: 2, replacesLeaseId: 'lease-1',
      jobId: 'job-77', instanceId: 'inst-2', nodeId: 'node-A'
    });
    assert.equal(world.leases.get('lease-1').status, 'REPLACED');

    // 4. 新实例凭自己的测量+租约 v2 通过审批，绑定版本 2
    const retryApproval = assertAllow(
      world.adjudicator.requestApproval(baseRequest({
        requestId: 'req-inst-2',
        instanceId: 'inst-2',
        measurementId: 'meas-2',
        leaseId: 'lease-2'
      })),
      'APPROVAL'
    );
    assert.equal(retryApproval.basis.bindings.leaseVersion, 2);

    // 5. 发布许可撤销：尚未消费的审批在签发时被阻断
    world.revocations.publish({
      scope: 'PERMISSION',
      serviceIdentity: IDENTITY_A,
      permission: 'billing:read'
    });
    assertDeny(
      world.adjudicator.issue({
        approvalId: retryApproval.approvalId,
        requestId: 'req-inst-2',
        leaseVersion: 2,
        audience: 'billing-api'
      }),
      'PERMISSION_REVOKED',
      'ISSUANCE'
    );

    // 6. 试图回头重放原实例已消费的审批
    assertDeny(
      world.adjudicator.issue({
        approvalId: first.approval.approvalId,
        requestId: 'req-inst-1',
        leaseVersion: 1,
        audience: 'billing-api'
      }),
      'APPROVAL_ALREADY_CONSUMED',
      'ISSUANCE'
    );

    // 7. 更换为未被撤销的许可与另一合法受众，重新走审批+签发成功
    const recovered = approveAndIssue(world, baseRequest({
      requestId: 'req-inst-2-recovered',
      instanceId: 'inst-2',
      measurementId: 'meas-2',
      leaseId: 'lease-2',
      audience: 'ledger-api',
      permissions: ['billing:charge']
    }));
    assert.deepEqual(recovered.issuance.credential.permissions, ['billing:charge']);

    // 8. 决策日志完整有序：5 次 ALLOW + 3 次 DENY
    const log = world.adjudicator.getDecisionLog();
    assert.equal(log.length, 8);
    assert.equal(log.filter((d) => d.verdict === 'ALLOW').length, 5);
    assert.equal(log.filter((d) => d.verdict === 'DENY').length, 3);
    assert.ok(log.every((d) => d.evaluatedAt && d.decisionId));
    assert.ok(log.filter((d) => d.verdict === 'DENY').every((d) => d.reasons.length === 1));

    // 9. 按请求 ID 追溯整条链路
    const retryChain = world.adjudicator.getDecisionsForRequest('req-inst-2');
    assert.deepEqual(retryChain.map((d) => d.verdict), ['ALLOW', 'DENY']);
  });

  test('同一节点可并存不同构建作业：各自测量/租约/身份独立裁决', () => {
    const world = seedWorld();
    world.identities.put({
      serviceIdentity: IDENTITY_B,
      imageDigest: DIGEST_B,
      allowedAudiences: ['reconcile-api'],
      permissions: ['ledger:reconcile']
    });
    world.measurements.put({
      measurementId: 'meas-3',
      jobId: 'job-88', instanceId: 'inst-9', nodeId: 'node-A',
      imageDigest: DIGEST_B, verified: true
    });
    world.leases.put({
      leaseId: 'lease-3', version: 1,
      jobId: 'job-88', instanceId: 'inst-9', nodeId: 'node-A'
    });

    approveAndIssue(world, {
      requestId: 'req-job-88',
      jobId: 'job-88', instanceId: 'inst-9', nodeId: 'node-A',
      measurementId: 'meas-3', leaseId: 'lease-3',
      serviceIdentity: IDENTITY_B, audience: 'reconcile-api',
      permissions: ['ledger:reconcile']
    });
    // 原作业在同一节点上的申请不受影响
    approveAndIssue(world, baseRequest({ requestId: 'req-job-77-still-ok' }));
  });
});

// ---------------------------------------------------------------------------
describe('输入防御', () => {
  test('畸形审批请求抛出 TypeError 而不是产生裁决', () => {
    const world = seedWorld();
    assert.throws(() => world.adjudicator.requestApproval(null), TypeError);
    assert.throws(
      () => world.adjudicator.requestApproval(baseRequest({ audience: '' })),
      TypeError
    );
    assert.throws(
      () => world.adjudicator.requestApproval(baseRequest({ permissions: 'nope' })),
      TypeError
    );
    assert.throws(
      () => world.adjudicator.issue({ approvalId: '' }),
      TypeError
    );
    assert.equal(world.adjudicator.getDecisionLog().length, 0);
  });
});
