import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { evaluateGate, ReasonCode } from '../src/index.js';
import {
  APPROVAL_SERVICE,
  EXECUTION_SERVICE,
  buildGateInput,
  clone
} from './fixtures/baseline.js';

const hasCode = (findings, code) => findings.some((f) => f.code === code);

describe('执行前门禁 · 端到端交叉约束', () => {
  it('可接受结果：范围收窄 + 安全选项可跨版本映射 + 条件默认值不扩权 -> ALLOW', () => {
    const result = evaluateGate(buildGateInput());
    assert.equal(result.decision, 'ALLOW', JSON.stringify(result.findings, null, 2));
    assert.equal(result.findings.length, 0);

    // op-1 执行侧显式收窄为只读
    const op1 = result.effective.execution.find((r) => r.id === 'op-1');
    assert.deepEqual(op1.permissions, ['read']);
    // v3 安全词汇已映射回审批侧 v2 词汇
    assert.equal(op1.encryption, 'AES256');

    // op-2 缺省字段由条件默认值还原，且未超过审批范围
    const op2 = result.effective.execution.find((r) => r.id === 'op-2');
    assert.deepEqual(op2.permissions, ['read']);
    assert.equal(op2.encryption, 'NONE');
    assert.equal(op2.retentionDays, 30);
    assert.ok(result.effective.appliedDefaults.execution.some((d) => d.path === 'permissions' && d.businessId === 'op-2'));
  });

  it('边界：执行范围与审批范围完全相等（非收窄但未扩大）-> ALLOW', () => {
    const input = buildGateInput();
    input.execution.parse[0].permissions = ['read', 'write'];
    const result = evaluateGate(input);
    assert.equal(result.decision, 'ALLOW', JSON.stringify(result.findings, null, 2));
  });

  it('边界：set 范围多一个 admin 元素即扩大 -> BLOCK(SCOPE_WIDENED)，定位到执行侧字段', () => {
    const input = buildGateInput();
    input.execution.parse[0].permissions = ['read', 'write', 'admin'];
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    const finding = result.findings.find((f) => f.code === ReasonCode.SCOPE_WIDENED);
    assert.ok(finding);
    assert.equal(finding.source, 'execution-parse');
    assert.match(finding.location, /businessId=op-1/);
    assert.match(finding.location, /permissions/);
  });

  it('应阻止：执行侧把目标 region 改成别的区域 -> BLOCK(TARGET_MISMATCH)', () => {
    const input = buildGateInput();
    input.execution.parse[0].target.region = 'cn-east-2';
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    const finding = result.findings.find((f) => f.code === ReasonCode.TARGET_MISMATCH);
    assert.ok(finding);
    assert.equal(finding.source, 'execution-parse');
    assert.equal(finding.actual, 'cn-east-2');
    assert.equal(finding.approved, 'cn-north-1');
    assert.match(finding.location, /target\.region/);
  });

  it('应阻止：实际操作与审批不一致 -> BLOCK(OP_MISMATCH)', () => {
    const input = buildGateInput();
    input.execution.parse[0].action = 'delete';
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    assert.ok(hasCode(result.findings, ReasonCode.OP_MISMATCH));
  });

  it('应阻止：同一侧清单出现重复业务标识 -> BLOCK(DUPLICATE_IDENTIFIER)，并给出两个位置', () => {
    const input = buildGateInput();
    input.execution.parse.push(clone(input.execution.parse[0]));
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    const finding = result.findings.find((f) => f.code === ReasonCode.DUPLICATE_IDENTIFIER);
    assert.ok(finding);
    assert.equal(finding.source, 'execution-parse');
    assert.deepEqual(finding.positions, [0, 2]);
  });

  it('应阻止：执行侧新增审批清单不存在的标识 -> BLOCK(IDENTIFIER_CONFLICT)', () => {
    const input = buildGateInput();
    input.execution.parse.push({
      id: 'op-rogue',
      action: 'read',
      target: { resourceId: 'bucket-x/secret', region: 'cn-north-1' },
      permissions: ['read'],
      encryption: 'DISABLED',
      retentionDays: 30
    });
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    const finding = result.findings.find((f) => f.code === ReasonCode.IDENTIFIER_CONFLICT);
    assert.ok(finding);
    assert.match(finding.message, /op-rogue/);
  });

  it('应阻止：执行侧漏掉审批清单中的标识 -> BLOCK(IDENTIFIER_MISALIGNED)', () => {
    const input = buildGateInput();
    input.execution.parse = input.execution.parse.filter((r) => r.id !== 'op-2');
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    assert.ok(hasCode(result.findings, ReasonCode.IDENTIFIER_MISALIGNED));
  });

  it('应阻止：条件默认值扩大权限（delete 默认 admin，审批只给 read）-> BLOCK(DEFAULT_EXPANDED)', () => {
    const input = buildGateInput();
    // 审批侧把 op-2 固定为 read/read；执行侧把动作改成 delete 且不给 permissions，
    // v3 语义表的 delete 条件默认值会还原出 ['admin']，越过审批范围。
    input.execution.parse[1] = {
      id: 'op-2',
      action: 'delete',
      target: { resourceId: 'bucket-a/object-2', region: 'cn-north-1' }
    };
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    const expanded = result.findings.find((f) => f.code === ReasonCode.DEFAULT_EXPANDED);
    assert.ok(expanded, '应明确指出是默认值扩大了权限');
    assert.equal(expanded.source, 'execution-parse');
    assert.equal(expanded.viaDefault, true);
    // 同一条记录同时存在操作不一致，门禁必须把全部差异来源都列出来
    assert.ok(hasCode(result.findings, ReasonCode.OP_MISMATCH));
  });

  it('边界/交错：默认值扩大权限时即使其他字段都合规也必须整体 BLOCK，且台账仍记录有效回执', () => {
    const input = buildGateInput();
    input.execution.parse[1] = {
      id: 'op-2',
      action: 'delete',
      target: { resourceId: 'bucket-a/object-2', region: 'cn-north-1' }
    };
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    assert.equal(result.ledger[EXECUTION_SERVICE].versionLabel, 'v3');
    assert.equal(result.ledger[APPROVAL_SERVICE].versionLabel, 'v2');
  });

  it('应阻止：安全选项无法跨版本映射（未知加密套件）-> BLOCK(SECURITY_UNMAPPED)', () => {
    const input = buildGateInput();
    // v3 枚举外的值会先在类型还原阶段被拒；这里直接构造“枚举内但映射表缺项”的场景：
    // 把 v3 枚举扩成包含新套件，但不提供到 v2 的映射。
    const encField = input.semanticTables[EXECUTION_SERVICE][1].fields.find((f) => f.path === 'encryption');
    encField.enum.push('AES-256-PQ');
    input.execution.parse[0].encryption = 'AES-256-PQ';
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    const finding = result.findings.find((f) => f.code === ReasonCode.SECURITY_UNMAPPED);
    assert.ok(finding);
    assert.equal(finding.badValue, 'AES-256-PQ');
    assert.equal(finding.fieldRole, 'security');
  });

  it('应阻止：迟到的旧版本执行回执(v2)不能替代当前 v3 结果 -> BLOCK(RECEIPT_STALE)', () => {
    const input = buildGateInput();
    input.receipts[1].versionLabel = 'v2';
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    const stale = result.findings.find((f) => f.code === ReasonCode.RECEIPT_STALE);
    assert.ok(stale);
    assert.match(stale.message, /旧解析回执不替代当前结果/);
    // 失效回执不推进台账
    assert.equal(result.ledger[EXECUTION_SERVICE], undefined);
    assert.ok(result.ledger[APPROVAL_SERVICE]);
  });

  it('应阻止：回执绑定别的请求摘要 -> BLOCK(RECEIPT_DIGEST_MISMATCH)，拒绝串单', () => {
    const input = buildGateInput();
    input.receipts[0].digest = 'sha256:other-request';
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    assert.ok(hasCode(result.findings, ReasonCode.RECEIPT_DIGEST_MISMATCH));
    assert.equal(result.ledger[APPROVAL_SERVICE], undefined);
  });

  it('交错：台账已登记 sequence=1，重复/倒退回执被拒，sequence=2 的新回执被接受', () => {
    const ledger = {
      [EXECUTION_SERVICE]: {
        digest: buildGateInput().digest,
        versionLabel: 'v3',
        versionOrdinal: 3,
        sequence: 1,
        receivedAt: '2026-11-19T09:59:00.000Z'
      }
    };

    const replayed = buildGateInput({ ledger: clone(ledger) });
    replayed.receipts[1].sequence = 1;
    const replayResult = evaluateGate(replayed);
    assert.equal(replayResult.decision, 'BLOCK');
    assert.ok(hasCode(replayResult.findings, ReasonCode.RECEIPT_DUPLICATE));
    // 被拒回执不得回退台账
    assert.equal(replayResult.ledger[EXECUTION_SERVICE].sequence, 1);

    const fresh = buildGateInput({ ledger: clone(ledger) });
    fresh.receipts[1].sequence = 2;
    const freshResult = evaluateGate(fresh);
    assert.equal(freshResult.decision, 'ALLOW');
    assert.equal(freshResult.ledger[EXECUTION_SERVICE].sequence, 2);
  });

  it('应阻止：缺少任一服务当前版本回执 -> BLOCK(RECEIPT_MISSING)', () => {
    const input = buildGateInput();
    input.receipts = input.receipts.filter((r) => r.service !== EXECUTION_SERVICE);
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    assert.ok(hasCode(result.findings, ReasonCode.RECEIPT_MISSING));
  });

  it('应阻止：审批侧解析结果与审批覆盖参数自相矛盾 -> BLOCK(COVERAGE_MISMATCH)', () => {
    const input = buildGateInput();
    input.approval.coverage[0].permissions = ['read', 'write', 'admin'];
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    const finding = result.findings.find((f) => f.code === ReasonCode.COVERAGE_MISMATCH);
    assert.ok(finding);
    assert.equal(finding.source, 'approval-parse');
  });

  it('应阻止：类型违规（permissions 收到对象而非集合）-> BLOCK(TYPE_VIOLATION)', () => {
    const input = buildGateInput();
    input.execution.parse[0].permissions = { read: true };
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    assert.ok(hasCode(result.findings, ReasonCode.TYPE_VIOLATION));
  });

  it('应阻止：出现语义表未声明的字段（失败关闭，不静默忽略）-> BLOCK(UNDECLARED_FIELD)', () => {
    const input = buildGateInput();
    input.execution.parse[0].impersonate = 'root';
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    const finding = result.findings.find((f) => f.code === ReasonCode.UNDECLARED_FIELD);
    assert.ok(finding);
    assert.equal(finding.location, 'impersonate');
  });

  it('交错：一次调用同时命中多种差异时全部上报，且每条 finding 都带来源与定位', () => {
    const input = buildGateInput();
    input.execution.parse[0].target.region = 'eu-west-1';
    input.execution.parse[0].permissions = ['admin'];
    input.receipts[1].versionLabel = 'v2';
    const result = evaluateGate(input);
    assert.equal(result.decision, 'BLOCK');
    assert.ok(hasCode(result.findings, ReasonCode.TARGET_MISMATCH));
    assert.ok(hasCode(result.findings, ReasonCode.SCOPE_WIDENED));
    assert.ok(hasCode(result.findings, ReasonCode.RECEIPT_STALE));
    for (const finding of result.findings) {
      assert.equal(typeof finding.code, 'string');
      assert.equal(typeof finding.source, 'string');
      assert.equal(typeof finding.location, 'string');
      assert.equal(typeof finding.message, 'string');
    }
  });

  it('纯函数保证：调用不修改入参 ledger，回执台账以新对象返回', () => {
    const input = buildGateInput();
    const snapshot = clone(input.ledger);
    const result = evaluateGate(input);
    assert.deepEqual(input.ledger, snapshot);
    assert.notEqual(result.ledger, input.ledger);
  });
});
