import { describe, test } from 'node:test';
import assert from 'node:assert/strict';
import { AdvisoryRegistry, sectionDigest } from '../src/registry.js';
import { AdvisoryError } from '../src/errors.js';

/**
 * 构造带可控时钟的注册表，并预置两个产品与两个已确认修复版本、已绑定批准的段落。
 */
function setupTwoProductScenario(startTime = new Date('2026-11-20T08:00:00.000Z')) {
  let now = new Date(startTime);
  const clock = () => now;
  const registry = new AdvisoryRegistry({ now: clock });
  const advance = (ms) => {
    now = new Date(now.getTime() + ms);
  };
  const setTime = (t) => {
    now = new Date(t);
  };

  registry.registerProduct('prod-a', '网关产品 A');
  registry.registerProduct('prod-b', '终端产品 B');

  const secA = registry.createSection('prod-a', 'A 产品漏洞描述 v1');
  const secB = registry.createSection('prod-b', 'B 产品漏洞描述 v1');
  registry.setFixVersion(secA.id, 'A-2.3.4');
  registry.setFixVersion(secB.id, 'B-7.8.9');
  registry.bindApproval(secA.id, 'security-lead', 'A 修复验证通过');
  registry.bindApproval(secB.id, 'security-lead', 'B 修复验证通过');

  return {
    registry, clock, advance, setTime, now: () => now,
    secA, secB,
  };
}

const PUBLISH_AT = '2026-11-20T10:00:00.000Z';

describe('通告放行模块', () => {
  // ============================================================ 可接受结果
  test('可接受结果：多产品合并通告逐段确认并到达公开时间后放行，按受控范围生成副本', () => {
    const { registry, advance, secA, secB } = setupTwoProductScenario();

    // r1 可看 A、B；r2 仅可看 B
    const advisory = registry.createAdvisory({
      title: '联合安全通告 2026-11',
      publishAt: PUBLISH_AT,
      sectionIds: [secA.id, secB.id],
      recipients: [
        { id: 'r1', productScope: ['prod-a', 'prod-b'] },
        { id: 'r2', productScope: ['prod-b'] },
      ],
    });

    // 公开时间前：评估应被阻止
    const before = registry.evaluateRelease(advisory.id);
    assert.equal(before.releasable, false);
    assert.ok(before.reasons.some((r) => r.code === 'PUBLIC_TIME_NOT_REACHED'));
    assert.ok(before.reasons.some((r) => r.code === 'SECTION_CONFIRMATION_REQUIRED'));
    assert.throws(
      () => registry.buildReleasePackage(advisory.id),
      (err) => err instanceof AdvisoryError && err.code === 'RELEASE_BLOCKED'
        && Array.isArray(err.reasons) && err.reasons.length >= 2,
    );

    // 逐段确认（合并通告必须按段落确认）
    registry.confirmSection(advisory.id, secA.id, 'release-manager');
    registry.confirmSection(advisory.id, secB.id, 'release-manager');

    // 仍未到公开时间
    assert.equal(registry.evaluateRelease(advisory.id).releasable, false);

    // 到达公开时间：放行
    advance(2 * 60 * 60 * 1000 + 5);
    const evaluation = registry.evaluateRelease(advisory.id);
    assert.deepEqual(evaluation.reasons, []);
    assert.equal(evaluation.releasable, true);

    const pkg = registry.buildReleasePackage(advisory.id);
    assert.equal(pkg.paragraphs.length, 2);
    // 明确断言每个段落的受控收件范围
    const paraA = pkg.paragraphs.find((p) => p.sectionId === secA.id);
    const paraB = pkg.paragraphs.find((p) => p.sectionId === secB.id);
    assert.deepEqual(paraA.recipientIds, ['r1']);
    assert.deepEqual(paraB.recipientIds.sort(), ['r1', 'r2']);
    assert.match(paraA.digest, /^sha256:[0-9a-f]{64}$/);

    // 发送：r1×2 + r2×1 = 3 个 queued 副本
    const sent = registry.send(advisory.id);
    assert.equal(sent.enqueuedCopies.length, 3);
    assert.ok(sent.enqueuedCopies.every((c) => c.status === 'queued'));
    assert.equal(registry.getAdvisory(advisory.id).status, 'sent');

    // 查看控制：获准段落可看，越权段落拒绝
    assert.equal(registry.canViewSection(advisory.id, 'r1', secA.id).allowed, true);
    assert.equal(registry.canViewSection(advisory.id, 'r1', secB.id).allowed, true);
    const denied = registry.canViewSection(advisory.id, 'r2', secA.id);
    assert.equal(denied.allowed, false);
    assert.match(denied.reason, /越权/);
    assert.equal(registry.canViewSection(advisory.id, 'r2', secB.id).allowed, true);

    // 追溯快照：原内容摘要 + 原始收件范围
    const snapshot = registry.getSentSnapshot(advisory.id);
    assert.equal(snapshot.sections.length, 2);
    assert.deepEqual(
      snapshot.recipientScope.map((r) => r.id).sort(),
      ['r1', 'r2'],
    );
    assert.equal(snapshot.sections[0].digest, paraA.digest);
    assert.equal(snapshot.sections.find((s) => s.sectionId === secB.id).fixVersion, 'B-7.8.9');
  });

  // ============================================================ 应被阻止：准入条件
  test('应被阻止：修复版本未确认、批准未绑定、改版后旧批准失效均拒绝放行', () => {
    const { registry, secA, secB } = setupTwoProductScenario();
    const raw = registry.createSection('prod-a', '尚未确认修复版本的段落');

    const advisory = registry.createAdvisory({
      title: '准入条件检查通告',
      publishAt: PUBLISH_AT,
      sectionIds: [raw.id, secB.id],
      recipients: [{ id: 'r1', productScope: ['prod-a', 'prod-b'] }],
    });

    // 修复版本未确认：确认动作本身被拒绝
    assert.throws(
      () => registry.confirmSection(advisory.id, raw.id, 'mgr'),
      (e) => e.code === 'FIX_VERSION_REQUIRED',
    );

    registry.confirmSection(advisory.id, secB.id, 'mgr');
    let reasons = registry.evaluateRelease(advisory.id).reasons.map((r) => r.code);
    assert.ok(reasons.includes('FIX_VERSION_REQUIRED'));
    assert.ok(reasons.includes('SECTION_CONFIRMATION_REQUIRED'));

    // 补上修复版本后仍缺批准
    registry.setFixVersion(raw.id, 'A-9.9.9');
    assert.throws(
      () => registry.confirmSection(advisory.id, raw.id, 'mgr'),
      (e) => e.code === 'APPROVAL_REQUIRED',
    );

    // 绑定批准 + 确认；随后改版使批准与确认双双失效
    registry.bindApproval(raw.id, 'lead');
    registry.confirmSection(advisory.id, raw.id, 'mgr');
    registry.reviseSection(raw.id, 'A 产品漏洞描述 v2（补充缓解措施）');

    const evaluation = registry.evaluateRelease(advisory.id);
    assert.equal(evaluation.releasable, false);
    const codes = evaluation.reasons.map((r) => r.code);
    assert.ok(codes.includes('APPROVAL_REQUIRED'), '改版后旧批准应失效');
    const stale = evaluation.reasons.find(
      (r) => r.code === 'SECTION_CONFIRMATION_REQUIRED' && r.sectionId === raw.id,
    );
    assert.ok(stale, '改版后旧确认应按过期处理');
    assert.match(stale.message, /须重新确认/);

    // 重新绑定批准并重新确认后，段落级阻断消除（仅剩公开时间）
    registry.bindApproval(raw.id, 'lead', '改版重新批准');
    registry.confirmSection(advisory.id, raw.id, 'mgr');
    const after = registry.evaluateRelease(advisory.id);
    assert.equal(
      after.reasons.some((r) => r.sectionId === raw.id),
      false,
    );
  });

  test('应被阻止：合并通告只确认部分段落时无法发送', () => {
    const { registry, secA, secB } = setupTwoProductScenario();
    const advisory = registry.createAdvisory({
      title: '逐段确认检查',
      publishAt: new Date('2026-11-20T09:00:00.000Z'),
      sectionIds: [secA.id, secB.id],
      recipients: [{ id: 'r1', productScope: ['prod-a', 'prod-b'] }],
    });
    registry.confirmSection(advisory.id, secA.id, 'mgr');
    const evaluation = registry.evaluateRelease(advisory.id);
    assert.equal(evaluation.releasable, false);
    assert.deepEqual(
      evaluation.reasons
        .filter((r) => r.code === 'SECTION_CONFIRMATION_REQUIRED')
        .map((r) => r.sectionId),
      [secB.id],
    );
    assert.throws(
      () => registry.send(advisory.id),
      (e) => e.code === 'RELEASE_BLOCKED',
    );
  });

  // ============================================================ 边界：公开时间
  test('边界：公开时间前一毫秒拒绝查看与放行，到达公开时刻立即放行', () => {
    const { registry, setTime, secA } = setupTwoProductScenario();
    const advisory = registry.createAdvisory({
      title: '时间边界通告',
      publishAt: PUBLISH_AT,
      sectionIds: [secA.id],
      recipients: [{ id: 'r1', productScope: ['prod-a'] }],
    });
    registry.confirmSection(advisory.id, secA.id, 'mgr');

    setTime(new Date('2026-11-20T09:59:59.999Z'));
    assert.equal(registry.evaluateRelease(advisory.id).releasable, false);
    assert.equal(registry.canViewSection(advisory.id, 'r1', secA.id).allowed, false);

    setTime(new Date(PUBLISH_AT));
    assert.equal(registry.evaluateRelease(advisory.id).releasable, true);
    assert.equal(registry.canViewSection(advisory.id, 'r1', secA.id).allowed, true);
  });

  // ============================================================ 应被阻止：受控收件范围
  test('应被阻止：收件范围越权在创建与追加时均被拒绝，查看控制拦截未获准段落', () => {
    const { registry, secA, secB } = setupTwoProductScenario();
    registry.registerProduct('prod-c', '第三方产品 C');

    assert.throws(
      () => registry.createAdvisory({
        title: '越权创建',
        publishAt: PUBLISH_AT,
        sectionIds: [secA.id],
        recipients: [{ id: 'rX', productScope: ['prod-a', 'prod-c'] }],
      }),
      (e) => e.code === 'RECIPIENT_OUT_OF_SCOPE' && e.productId === 'prod-c',
    );

    const advisory = registry.createAdvisory({
      title: '受控范围通告',
      publishAt: PUBLISH_AT,
      sectionIds: [secA.id, secB.id],
      recipients: [{ id: 'r1', productScope: ['prod-a'] }],
    });

    assert.throws(
      () => registry.addRecipient(advisory.id, 'r2', ['prod-b', 'prod-c']),
      (e) => e.code === 'RECIPIENT_OUT_OF_SCOPE',
    );
    assert.throws(
      () => registry.addRecipient(advisory.id, 'r1', ['prod-b']),
      (e) => e.code === 'RECIPIENT_EXISTS',
    );

    // 段落没有任何获准收件人时，即使其他条件满足也不能构建通告包
    registry.confirmSection(advisory.id, secA.id, 'mgr');
    registry.confirmSection(advisory.id, secB.id, 'mgr');
    // 段落没有任何获准收件人时，即使其他条件满足也不能构建通告包：
    // 用独立时钟场景（已过公开时间）验证 NO_ALLOWED_RECIPIENTS
    const { registry: reg2, setTime: set2, secA: a2, secB: b2 } = setupTwoProductScenario();
    set2(new Date('2026-11-20T10:00:01.000Z'));
    const adv2 = reg2.createAdvisory({
      title: '无有效收件人',
      publishAt: PUBLISH_AT,
      sectionIds: [a2.id, b2.id],
      recipients: [{ id: 'r1', productScope: ['prod-a'] }],
    });
    reg2.confirmSection(adv2.id, a2.id, 'mgr');
    reg2.confirmSection(adv2.id, b2.id, 'mgr');
    assert.throws(
      () => reg2.send(adv2.id),
      (e) => e.code === 'NO_ALLOWED_RECIPIENTS' && e.sectionId === b2.id,
    );
  });

  // ============================================================ 交错：移除收件人与副本作废
  test('交错场景：发送后移除收件人，未出队副本作废、已交付保留、作废副本禁止出队', () => {
    const { registry, advance, secA, secB } = setupTwoProductScenario();
    const advisory = registry.createAdvisory({
      title: '副本作废通告',
      publishAt: PUBLISH_AT,
      sectionIds: [secA.id, secB.id],
      recipients: [
        { id: 'r1', productScope: ['prod-a', 'prod-b'] },
        { id: 'r2', productScope: ['prod-a', 'prod-b'] },
      ],
    });
    registry.confirmSection(advisory.id, secA.id, 'mgr');
    registry.confirmSection(advisory.id, secB.id, 'mgr');
    // 推进到公开时间之后再发送
    advance(3 * 60 * 60 * 1000);
    // 直接把时钟拨到公开时间
    const sent = registry.send(advisory.id);
    assert.equal(sent.enqueuedCopies.length, 4);

    // r1 先出队 A 段落（delivered）
    registry.dequeueCopy(advisory.id, 'r1', secA.id);

    // 移除 r2：其两个 queued 副本全部作废
    const removeR2 = registry.removeRecipient(advisory.id, 'r2');
    assert.equal(removeR2.voidedCopies.length, 2);
    assert.ok(removeR2.voidedCopies.every((c) => c.status === 'voided'));

    // 移除 r1：剩余 queued（B 段落）作废，已 delivered 的 A 段落保留
    const removeR1 = registry.removeRecipient(advisory.id, 'r1');
    assert.equal(removeR1.voidedCopies.length, 1);
    assert.equal(removeR1.voidedCopies[0].sectionId, secB.id);

    const copies = registry.listCopies(advisory.id);
    const count = (status) => copies.filter((c) => c.status === status).length;
    assert.equal(count('queued'), 0);
    assert.equal(count('delivered'), 1);
    assert.equal(count('voided'), 3);

    // 作废副本禁止出队
    assert.throws(
      () => registry.dequeueCopy(advisory.id, 'r2', secA.id),
      (e) => e.code === 'COPY_VOIDED',
    );
    // 已交付副本不能重复出队
    assert.throws(
      () => registry.dequeueCopy(advisory.id, 'r1', secA.id),
      (e) => e.code === 'COPY_NOT_QUEUED',
    );
    // 重复移除 / 未知副本
    assert.throws(
      () => registry.removeRecipient(advisory.id, 'r2'),
      (e) => e.code === 'RECIPIENT_NOT_FOUND',
    );
    assert.throws(
      () => registry.dequeueCopy(advisory.id, 'ghost', secA.id),
      (e) => e.code === 'UNKNOWN_COPY',
    );

    // 被移除收件人无法再查看；追溯快照仍保留原始收件范围
    assert.equal(registry.canViewSection(advisory.id, 'r2', secA.id).allowed, false);
    const snapshot = registry.getSentSnapshot(advisory.id);
    assert.deepEqual(
      snapshot.recipientScope.map((r) => r.id).sort(),
      ['r1', 'r2'],
    );
  });

  test('交错场景：发送前移除收件人，则发送时不为其生成任何副本', () => {
    const { registry, advance, secA, secB } = setupTwoProductScenario();
    const advisory = registry.createAdvisory({
      title: '发送前移除',
      publishAt: PUBLISH_AT,
      sectionIds: [secA.id, secB.id],
      recipients: [
        { id: 'r1', productScope: ['prod-a', 'prod-b'] },
        { id: 'r2', productScope: ['prod-b'] },
      ],
    });
    registry.removeRecipient(advisory.id, 'r2');
    registry.confirmSection(advisory.id, secA.id, 'mgr');
    registry.confirmSection(advisory.id, secB.id, 'mgr');
    advance(3 * 60 * 60 * 1000);

    const sent = registry.send(advisory.id);
    assert.deepEqual(
      sent.enqueuedCopies.map((c) => c.recipientId),
      ['r1', 'r1'],
    );
    assert.equal(registry.canViewSection(advisory.id, 'r2', secB.id).allowed, false);
    // 草稿阶段移除不产生作废副本记录
    assert.deepEqual(registry.listCopies(advisory.id).filter((c) => c.status === 'voided'), []);
  });

  // ============================================================ 已发送冻结与追溯
  test('追溯：已发送通告冻结写操作，段落改版后快照仍保留原内容摘要与收件范围', () => {
    const { registry, advance, secA, secB } = setupTwoProductScenario();
    const advisory = registry.createAdvisory({
      title: '冻结与追溯',
      publishAt: PUBLISH_AT,
      sectionIds: [secA.id, secB.id],
      recipients: [{ id: 'r1', productScope: ['prod-a', 'prod-b'] }],
    });
    registry.confirmSection(advisory.id, secA.id, 'mgr');
    registry.confirmSection(advisory.id, secB.id, 'mgr');
    advance(3 * 60 * 60 * 1000);
    const sent = registry.send(advisory.id);
    const snapshotBefore = registry.getSentSnapshot(advisory.id);
    const oldDigestA = snapshotBefore.sections.find((s) => s.sectionId === secA.id).digest;
    const baseRevisionA = snapshotBefore.sections.find((s) => s.sectionId === secA.id).revision;

    // 冻结：重复发送、追加收件人、再次确认均被拒绝
    assert.throws(() => registry.send(advisory.id), (e) => e.code === 'ALREADY_SENT');
    assert.throws(
      () => registry.addRecipient(advisory.id, 'r9', ['prod-a']),
      (e) => e.code === 'ALREADY_SENT',
    );
    assert.throws(
      () => registry.confirmSection(advisory.id, secA.id, 'mgr'),
      (e) => e.code === 'ALREADY_SENT',
    );

    // 段落本身可以改版（供新通告使用），但已发送快照保持修订 1 的原内容
    registry.reviseSection(secA.id, 'A 产品漏洞描述 v2（后续修订）');
    registry.setFixVersion(secA.id, 'A-2.4.0');
    const snapshotAfter = registry.getSentSnapshot(advisory.id);
    const snapA = snapshotAfter.sections.find((s) => s.sectionId === secA.id);
    assert.equal(snapA.revision, baseRevisionA);
    assert.equal(snapA.body, 'A 产品漏洞描述 v1');
    assert.equal(snapA.fixVersion, 'A-2.3.4');
    assert.equal(snapA.digest, oldDigestA);
    assert.deepEqual(snapshotAfter.recipientScope, snapshotBefore.recipientScope);

    // 未发送通告不存在追溯快照
    const { registry: reg2, secA: a2 } = setupTwoProductScenario();
    const adv2 = reg2.createAdvisory({
      title: '未发送',
      publishAt: PUBLISH_AT,
      sectionIds: [a2.id],
      recipients: [{ id: 'r1', productScope: ['prod-a'] }],
    });
    assert.throws(() => reg2.getSentSnapshot(adv2.id), (e) => e.code === 'NOT_SENT');

    // 摘要确定性：相同输入摘要一致，正文变更摘要变化
    const d1 = sectionDigest('prod-a', 'body', 'A-1.0.0');
    const d2 = sectionDigest('prod-a', 'body', 'A-1.0.0');
    const d3 = sectionDigest('prod-a', 'body-changed', 'A-1.0.0');
    assert.equal(d1, d2);
    assert.notEqual(d1, d3);

    // 发送结果包含追溯所需的完整数据
    assert.equal(sent.advisory.sentAt, snapshotBefore.sentAt);
  });

  // ============================================================ 综合交叉约束
  test('综合交叉：确认后变更修复版本 → 批准与确认同时失效，重新绑定并确认后方可发送', () => {
    const { registry, advance, secA } = setupTwoProductScenario();
    const advisory = registry.createAdvisory({
      title: '修复版本变更重放行',
      publishAt: PUBLISH_AT,
      sectionIds: [secA.id],
      recipients: [{ id: 'r1', productScope: ['prod-a'] }],
    });
    registry.confirmSection(advisory.id, secA.id, 'mgr');
    const baseRevision = registry.getSection(secA.id).revision;

    // 修复版本变更：修订号推进、批准清空
    const updated = registry.setFixVersion(secA.id, 'A-2.3.5');
    assert.equal(updated.revision, baseRevision + 1);
    assert.deepEqual(updated.approvals, []);

    const blocked = registry.evaluateRelease(advisory.id);
    assert.equal(blocked.releasable, false);
    const codes = blocked.reasons.map((r) => r.code);
    assert.ok(codes.includes('APPROVAL_REQUIRED'));
    assert.ok(codes.includes('SECTION_CONFIRMATION_REQUIRED'));
    assert.throws(() => registry.send(advisory.id), (e) => e.code === 'RELEASE_BLOCKED');

    // 未重新批准前不能确认
    assert.throws(
      () => registry.confirmSection(advisory.id, secA.id, 'mgr'),
      (e) => e.code === 'APPROVAL_REQUIRED',
    );

    registry.bindApproval(secA.id, 'lead', '新修复版本验证通过');
    registry.confirmSection(advisory.id, secA.id, 'mgr');
    // 时间仍未到，唯一阻断应为公开时间
    const onlyTime = registry.evaluateRelease(advisory.id).reasons.map((r) => r.code);
    assert.deepEqual(onlyTime, ['PUBLIC_TIME_NOT_REACHED']);

    // 到达公开时间后成功发送，快照绑定修订 2
    advance(3 * 60 * 60 * 1000);
    const sent = registry.send(advisory.id);
    assert.equal(sent.advisory.status, 'sent');
    const snapshot = registry.getSentSnapshot(advisory.id);
    const snapA = snapshot.sections.find((s) => s.sectionId === secA.id);
    assert.equal(snapA.revision, baseRevision + 1);
    assert.equal(snapA.fixVersion, 'A-2.3.5');
    assert.match(snapA.digest, /^sha256:[0-9a-f]{64}$/);
    assert.deepEqual(snapshot.recipientScope.map((r) => r.id), ['r1']);

    // 重新确认的记录落在新修订上
    const view = registry.getAdvisory(advisory.id);
    assert.equal(view.confirmations[0].revision, baseRevision + 1);
  });

  test('边界：合并通告段落重复、重复注册产品、非法输入均被拦截', () => {
    const { registry, secA } = setupTwoProductScenario();
    assert.throws(
      () => registry.registerProduct('prod-a', '重复产品'),
      (e) => e.code === 'PRODUCT_EXISTS',
    );
    assert.throws(
      () => registry.createAdvisory({
        title: '重复段落',
        publishAt: PUBLISH_AT,
        sectionIds: [secA.id, secA.id],
        recipients: [{ id: 'r1', productScope: ['prod-a'] }],
      }),
      (e) => e.code === 'DUPLICATE_SECTION',
    );
    assert.throws(
      () => registry.createAdvisory({
        title: '坏时间',
        publishAt: 'not-a-date',
        sectionIds: [secA.id],
        recipients: [{ id: 'r1', productScope: ['prod-a'] }],
      }),
      (e) => e.code === 'INVALID_INPUT',
    );
    assert.throws(
      () => registry.confirmSection('adv_none', secA.id, 'mgr'),
      (e) => e.code === 'UNKNOWN_ADVISORY',
    );
  });
});
