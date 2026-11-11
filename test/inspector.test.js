import { test } from 'node:test';
import assert from 'node:assert/strict';
import { sha256Hex, computeFieldDigest, inspectPair, ReportStore, ISSUE } from '../src/index.js';

/* ------------------------------------------------------------------ *
 * 测试数据构造辅助
 * ------------------------------------------------------------------ */

function d(text) {
  return sha256Hex(text);
}

/**
 * 字段级摘要：与 src/digest.js 的重建规则一致。
 * 安全校验记录按契约挂字段级摘要（多片段时为全部片段摘要的重建哈希）。
 */
function fd(...fragmentDigests) {
  return computeFieldDigest(fragmentDigests);
}

function frag(side, recordId, requestId, fieldName, fieldInstance, fragmentIndex, parseStatus, contentDigest, byteLength) {
  return {
    kind: 'fragment',
    side,
    recordId,
    requestId,
    fieldName,
    fieldInstance,
    fragmentIndex,
    parseStatus,
    contentDigest,
    byteLength: byteLength ?? (contentDigest ? contentDigest.length : null),
  };
}

function check(side, recordId, requestId, fieldName, fieldInstance, verdict, contentDigest) {
  return { kind: 'check', side, recordId, requestId, fieldName, fieldInstance, verdict, contentDigest };
}

function gatewayEvent(fragments, checks) {
  return { side: 'gateway', fragments, checks: checks ?? [] };
}
function businessEvent(fragments, checks) {
  return { side: 'business', fragments, checks: checks ?? [] };
}

function findIssues(report, type) {
  return report.issues.filter((i) => i.type === type);
}

function assertBlockShape(issue) {
  assert.ok(issue.issueId, '每个问题必须有稳定 issueId');
  assert.ok(issue.requestId && issue.fieldName, '必须给出请求号与字段名');
  assert.equal(typeof issue.fieldInstance, 'number');
  assert.ok(issue.blockReason.length > 0, '必须给出阻止提交原因');
  assert.ok(
    issue.gatewayRecordIds.length > 0 || issue.businessRecordIds.length > 0,
    '必须列出网关或业务端记录编号',
  );
}

/* 同名附件 attachment 的两个实例的片段摘要（两端共用）；单片段字段的字段级摘要即其重建值 */
const A1_DIGEST = d('attachment-1-body');
const A2_DIGEST = d('attachment-2-body');
const A1_FIELD = fd(A1_DIGEST);
const A2_FIELD = fd(A2_DIGEST);
const REQ = 'REQ-100';

function bothSidesTwoAttachments(overrides = {}) {
  const gwF = [
    frag('gateway', 'G-F1-0', REQ, 'attachment', 1, 0, 'ok', A1_DIGEST),
    frag('gateway', 'G-F2-0', REQ, 'attachment', 2, 0, 'ok', A2_DIGEST),
  ];
  const bizF = [
    frag('business', 'B-F1-0', REQ, 'attachment', 1, 0, 'ok', A1_DIGEST),
    frag('business', 'B-F2-0', REQ, 'attachment', 2, 0, 'ok', A2_DIGEST),
  ];
  const gwC = [
    check('gateway', 'G-C1', REQ, 'attachment', 1, 'allow', A1_FIELD),
    check('gateway', 'G-C2', REQ, 'attachment', 2, 'allow', A2_FIELD),
  ];
  const bizC = [
    check('business', 'B-C1', REQ, 'attachment', 1, 'allow', A1_FIELD),
    check('business', 'B-C2', REQ, 'attachment', 2, 'allow', A2_FIELD),
  ];
  return {
    gw: gatewayEvent(overrides.gwFragments ?? gwF, overrides.gwChecks ?? gwC),
    biz: businessEvent(overrides.bizFragments ?? bizF, overrides.bizChecks ?? bizC),
  };
}

/* ------------------------------------------------------------------ *
 * 场景一：合法的多附件请求（同名字段多实例），无问题、不阻止提交
 * ------------------------------------------------------------------ */

test('合法多附件请求：同名 attachment 字段两个实例完整对齐', () => {
  const { gw, biz } = bothSidesTwoAttachments();
  const report = inspectPair(gw, biz);

  assert.equal(report.issues.length, 0);
  assert.equal(report.blocking, false);
  assert.equal(report.reportId.length, 64);
  assert.equal(report.summary.validRequestCount, 1);

  const valid = report.validRequests[0];
  assert.equal(valid.requestId, REQ);
  const field = valid.fields.find((f) => f.fieldName === 'attachment');
  assert.equal(field.multiAttachment, true);
  assert.equal(field.instanceCount, 2);
  // 两端记录编号都在合法结果中可追溯
  assert.deepEqual(field.instances.map((i) => i.gatewayRecordIds[0]), ['G-F1-0', 'G-F2-0']);
  assert.deepEqual(field.instances.map((i) => i.businessRecordIds[0]), ['B-F1-0', 'B-F2-0']);
});

/* ------------------------------------------------------------------ *
 * 场景二：同名附件的顺序变化——业务端先解析到第二个附件
 * 重建按字段实例编号归位，顺序变化不得误报
 * ------------------------------------------------------------------ */

test('同名附件顺序变化：业务端片段到达顺序颠倒仍正确关联', () => {
  const { gw, biz } = bothSidesTwoAttachments();
  // 业务端故意把实例 2 的片段排在前面
  biz.fragments = [biz.fragments[1], biz.fragments[0]];
  biz.checks = [biz.checks[1], biz.checks[0]];

  const report = inspectPair(gw, biz);
  assert.equal(report.issues.length, 0, '按实例编号归位后不应产生任何问题');
  assert.equal(report.blocking, false);
});

/* ------------------------------------------------------------------ *
 * 场景三：不同附件共用字段实例编号——同一编号两端内容不同
 * 必须报 metadata_conflict，并列出两端记录编号与阻止原因
 * ------------------------------------------------------------------ */

test('关联冲突前置：同一字段实例编号两端内容摘要不一致', () => {
  const otherDigest = d('totally-different-attachment');
  const { gw, biz } = bothSidesTwoAttachments({
    bizFragments: [
      frag('business', 'B-F1-0', REQ, 'attachment', 1, 0, 'ok', A1_DIGEST),
      frag('business', 'B-F2-0', REQ, 'attachment', 2, 0, 'ok', otherDigest),
    ],
  });

  const report = inspectPair(gw, biz);
  const conflicts = findIssues(report, ISSUE.METADATA_CONFLICT);
  assert.ok(conflicts.length >= 1);

  const target = conflicts.find((i) => i.fieldInstance === 2);
  assert.ok(target);
  assertBlockShape(target);
  assert.deepEqual(target.gatewayRecordIds, ['G-F2-0']);
  assert.deepEqual(target.businessRecordIds, ['B-F2-0']);
  assert.match(target.blockReason, /内容摘要不一致/);
  assert.equal(report.blocking, true);
});

/* ------------------------------------------------------------------ *
 * 场景四：第二个附件被截断（业务端片段 truncated）
 * ------------------------------------------------------------------ */

test('传输截断：第二个附件业务端收到 truncated 片段', () => {
  const { gw, biz } = bothSidesTwoAttachments({
    bizFragments: [
      frag('business', 'B-F1-0', REQ, 'attachment', 1, 0, 'ok', A1_DIGEST),
      frag('business', 'B-F2-0', REQ, 'attachment', 2, 0, 'truncated', null),
    ],
    // 截断的附件无法产生校验记录
    bizChecks: [check('business', 'B-C1', REQ, 'attachment', 1, 'allow', A1_FIELD)],
  });

  const report = inspectPair(gw, biz);
  const trunc = findIssues(report, ISSUE.TRUNCATION);
  assert.equal(trunc.length, 1);
  assert.equal(trunc[0].fieldInstance, 2);
  assertBlockShape(trunc[0]);
  assert.deepEqual(trunc[0].businessRecordIds, ['B-F2-0']);
  assert.match(trunc[0].blockReason, /截断|解析错误/);

  // 截断附件同时缺少业务端校验记录 -> field_missing
  const missing = findIssues(report, ISSUE.FIELD_MISSING);
  const checkMissing = missing.find((i) => i.blockReason.includes('安全校验记录') && i.fieldInstance === 2);
  assert.ok(checkMissing, '截断附件的校验记录缺失必须单独报告');
  assert.deepEqual(checkMissing.gatewayRecordIds, ['G-C2']);
  assert.deepEqual(checkMissing.businessRecordIds, ['B-F2-0']);

  assert.equal(report.blocking, true);
});

/* ------------------------------------------------------------------ *
 * 场景五：业务端只收到部分校验记录（实例 2 的校验丢失）
 * ------------------------------------------------------------------ */

test('字段缺失：业务端只收到部分安全校验记录', () => {
  const { gw, biz } = bothSidesTwoAttachments({
    bizChecks: [check('business', 'B-C1', REQ, 'attachment', 1, 'allow', A1_FIELD)],
  });

  const report = inspectPair(gw, biz);
  const missing = findIssues(report, ISSUE.FIELD_MISSING);
  assert.equal(missing.length, 1);
  assert.equal(missing[0].fieldInstance, 2);
  assertBlockShape(missing[0]);
  assert.deepEqual(missing[0].gatewayRecordIds, ['G-C2']);
  assert.deepEqual(missing[0].businessRecordIds, ['B-F2-0']);
  assert.match(missing[0].blockReason, /安全校验记录/);
  assert.equal(report.blocking, true);
});

/* ------------------------------------------------------------------ *
 * 场景六：旧版缺陷——安全校验结果记到另一个附件上（错位归属）
 * 业务端把实例 1、2 的校验记录互换
 * ------------------------------------------------------------------ */

test('关联冲突：安全校验结果被错位归属到另一附件', () => {
  const { gw, biz } = bothSidesTwoAttachments({
    gwChecks: [
      check('gateway', 'G-C1', REQ, 'attachment', 1, 'allow', A1_FIELD),
      check('gateway', 'G-C2', REQ, 'attachment', 2, 'block', A2_FIELD),
    ],
    bizChecks: [
      // 互换：实例 1 挂了附件 2 的摘要和结论，实例 2 反之
      check('business', 'B-C1', REQ, 'attachment', 1, 'block', A2_FIELD),
      check('business', 'B-C2', REQ, 'attachment', 2, 'allow', A1_FIELD),
    ],
  });

  const report = inspectPair(gw, biz);
  const conflicts = findIssues(report, ISSUE.ATTRIBUTION_CONFLICT);
  assert.ok(conflicts.length >= 2, '两个被错位的实例都应被识别');

  const affectedInstances = new Set(conflicts.map((i) => i.fieldInstance));
  assert.ok(affectedInstances.has(1) && affectedInstances.has(2));

  for (const issue of conflicts) {
    assertBlockShape(issue);
    assert.ok(
      issue.gatewayRecordIds.includes('G-C1') || issue.gatewayRecordIds.includes('G-C2'),
      '必须给出网关侧校验记录编号',
    );
    assert.ok(
      issue.businessRecordIds.includes('B-C1') || issue.businessRecordIds.includes('B-C2'),
      '必须给出业务端侧校验记录编号',
    );
  }

  // 必须有一条明确指出"错位归属到其他附件"
  assert.ok(
    conflicts.some((i) => /错位归属|校验结果被记到|不一致/.test(i.blockReason)),
    '阻止原因需说明校验结果错位归属',
  );
  assert.equal(report.blocking, true);
});

/* ------------------------------------------------------------------ *
 * 场景七：重复导入幂等
 * ------------------------------------------------------------------ */

test('幂等：同一批结果重复导入不产生新问题，reportId/issueId 稳定', () => {
  const { gw, biz } = bothSidesTwoAttachments({
    bizChecks: [check('business', 'B-C1', REQ, 'attachment', 1, 'allow', A1_DIGEST)],
  });

  const first = inspectPair(gw, biz);
  const second = inspectPair(gw, biz);
  assert.equal(first.reportId, second.reportId, '纯函数报告 ID 必须稳定');
  assert.deepEqual(
    first.issues.map((i) => i.issueId),
    second.issues.map((i) => i.issueId),
  );

  const store = new ReportStore();
  const imported = store.importPair(gw, biz, 'BATCH-1');
  assert.equal(imported.imported, true);
  assert.equal(imported.newIssueCount, first.issues.length);

  const repeated = store.importPair(gw, biz, 'BATCH-1');
  assert.equal(repeated.deduped, true);
  assert.equal(repeated.newIssueCount, 0);
  assert.equal(repeated.reportId, imported.reportId);

  // 换批次号导入同一批内容：问题按 issueId 全局去重
  const anotherBatch = store.importPair(gw, biz, 'BATCH-2');
  assert.equal(anotherBatch.newIssueCount, 0);
  assert.equal(anotherBatch.skippedIssueCount, first.issues.length);

  // 同一批次号但内容变化：拒绝覆盖
  assert.throws(
    () => store.importPair(gw, businessEvent([], []), 'BATCH-1'),
    /不同内容导入/,
  );
});

/* ------------------------------------------------------------------ *
 * 场景八：校验记录引用不存在的字段实例编号（无处归属）
 * ------------------------------------------------------------------ */

test('关联冲突：校验记录引用不存在的字段实例', () => {
  const { gw, biz } = bothSidesTwoAttachments({
    bizChecks: [
      check('business', 'B-C1', REQ, 'attachment', 1, 'allow', A1_FIELD),
      check('business', 'B-C2', REQ, 'attachment', 2, 'allow', A2_FIELD),
      check('business', 'B-C9', REQ, 'attachment', 9, 'allow', fd(d('ghost'))),
    ],
  });

  const report = inspectPair(gw, biz);
  const conflicts = findIssues(report, ISSUE.ATTRIBUTION_CONFLICT);
  const dangling = conflicts.find((i) => i.fieldInstance === 9);
  assert.ok(dangling);
  assertBlockShape(dangling);
  assert.deepEqual(dangling.businessRecordIds, ['B-C9']);
  assert.match(dangling.blockReason, /不存在的字段实例/);
});

/* ------------------------------------------------------------------ *
 * 场景九：业务端整体缺少某字段实例（字段缺失，非截断）
 * ------------------------------------------------------------------ */

test('字段缺失：业务端完全没有第二个附件实例', () => {
  const { gw, biz } = bothSidesTwoAttachments({
    bizFragments: [frag('business', 'B-F1-0', REQ, 'attachment', 1, 0, 'ok', A1_DIGEST)],
    bizChecks: [check('business', 'B-C1', REQ, 'attachment', 1, 'allow', A1_FIELD)],
  });

  const report = inspectPair(gw, biz);
  // 实例 2 在业务端整体缺失：同时产生"字段实例缺失"与"校验记录缺失"两条问题
  const missing = findIssues(report, ISSUE.FIELD_MISSING).filter((i) => i.fieldInstance === 2);
  assert.ok(missing.length >= 1);
  for (const issue of missing) assertBlockShape(issue);
  assert.deepEqual([...new Set(missing.flatMap((i) => i.gatewayRecordIds))].sort(), ['G-C2', 'G-F2-0']);
  assert.deepEqual([...new Set(missing.flatMap((i) => i.businessRecordIds))], []);
  assert.ok(missing.some((i) => i.blockReason.includes('字段实例')));
  assert.equal(report.blocking, true);
});

/* ------------------------------------------------------------------ *
 * 场景十：多片段字段（编号有缺口 / 重复编号）的重建防护
 * ------------------------------------------------------------------ */

test('传输截断：片段编号缺口与重复编号判定为不完整', () => {
  const gw = gatewayEvent([
    frag('gateway', 'G-F1-0', REQ, 'blob', 1, 0, 'ok', d('p0')),
    frag('gateway', 'G-F1-2', REQ, 'blob', 1, 2, 'ok', d('p2')), // 缺口：缺 #1
  ]);
  const biz = businessEvent([
    frag('business', 'B-F1-0', REQ, 'blob', 1, 0, 'ok', d('p0')),
    frag('business', 'B-F1-1', REQ, 'blob', 1, 1, 'ok', d('p1')),
    frag('business', 'B-F1-1DUP', REQ, 'blob', 1, 1, 'ok', d('p1-dup')), // 重复编号
  ]);

  const report = inspectPair(gw, biz);
  // 片段数量先触发元数据冲突；两端各自的编号缺口/重复再触发传输截断
  assert.equal(report.blocking, true);
  assert.ok(findIssues(report, ISSUE.METADATA_CONFLICT).length >= 1);
  const trunc = findIssues(report, ISSUE.TRUNCATION);
  assert.ok(trunc.length >= 1, '编号缺口与重复编号必须作为传输截断类问题上报');
  assert.match(trunc.map((i) => i.blockReason).join(' '), /片段序列不完整/);
  for (const issue of report.issues) assertBlockShape(issue);
});

test('非法输入：记录缺字段、状态非法、编号重复时抛错', () => {
  assert.throws(
    () => inspectPair(gatewayEvent([{ recordId: 'X' }]), businessEvent([])),
    /缺少必填字段/,
  );
  assert.throws(
    () =>
      inspectPair(
        gatewayEvent([frag('gateway', 'G1', REQ, 'f', 1, 0, 'bogus', null)]),
        businessEvent([]),
      ),
    /parseStatus/,
  );
  assert.throws(
    () =>
      inspectPair(
        gatewayEvent([
          frag('gateway', 'DUP', REQ, 'f', 1, 0, 'ok', d('a')),
          frag('gateway', 'DUP', REQ, 'f', 1, 1, 'ok', d('b')),
        ]),
        businessEvent([]),
      ),
    /记录编号重复/,
  );
});
