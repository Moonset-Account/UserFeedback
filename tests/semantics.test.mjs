import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { ReasonCode, FieldRole, restoreOne, selectDefault, mapValueAcrossVersions, mapRecordToVersion, isScopeNarrower } from '../src/index.js';
import { approvalV2, executionV3, approvalV1 } from './fixtures/baseline.js';

describe('语义还原 · 类型与条件默认值', () => {
  it('set 接受数组与逗号分隔字符串，并去重排序', () => {
    const field = approvalV2.fields.find((f) => f.path === 'permissions');
    const a = restoreOne({ id: 'x', action: 'read', target: { resourceId: 'r', region: 'z' }, permissions: ['write', 'read', 'read'] }, approvalV2, 'test');
    assert.deepEqual(a.record.permissions, ['read', 'write']);
    const b = restoreOne({ id: 'x', action: 'read', target: { resourceId: 'r', region: 'z' }, permissions: 'read, write ,' }, approvalV2, 'test');
    assert.deepEqual(b.record.permissions, ['read', 'write']);
  });

  it('integer 拒绝布尔与小数；枚举拒绝表外值', () => {
    const r1 = restoreOne({ id: 'x', action: 'read', target: { resourceId: 'r', region: 'z' }, retentionDays: true }, approvalV2, 'test');
    assert.ok(r1.findings.some((f) => f.code === ReasonCode.TYPE_VIOLATION));
    const r2 = restoreOne({ id: 'x', action: 'read', target: { resourceId: 'r', region: 'z' }, retentionDays: 1.5 }, approvalV2, 'test');
    assert.ok(r2.findings.some((f) => f.code === ReasonCode.TYPE_VIOLATION));
    const r3 = restoreOne({ id: 'x', action: 'explode', target: { resourceId: 'r', region: 'z' } }, approvalV2, 'test');
    assert.ok(r3.findings.some((f) => f.code === ReasonCode.TYPE_VIOLATION));
  });

  it('条件默认值按首个命中规则选择：delete -> AES256，其它动作 -> NONE', () => {
    const encField = approvalV2.fields.find((f) => f.path === 'encryption');
    assert.equal(selectDefault(encField, { action: 'delete' }).value, 'AES256');
    assert.equal(selectDefault(encField, { action: 'read' }).value, 'NONE');
  });

  it('缺失字段经定点迭代由条件默认值还原，并记录 appliedDefaults', () => {
    const result = restoreOne({ id: 'op-2', action: 'read', target: { resourceId: 'r', region: 'z' } }, executionV3, 'execution-parse');
    assert.deepEqual(result.record.permissions, ['read']);
    assert.equal(result.record.encryption, 'DISABLED');
    assert.equal(result.record.retentionDays, 30);
    const paths = result.appliedDefaults.map((d) => d.path);
    assert.deepEqual(paths.sort(), ['encryption', 'permissions', 'retentionDays']);
    assert.equal(result.findings.length, 0);
  });

  it('依赖无法满足（条件字段缺失且无兜底）：默认值不应用，并以 DEFAULT_CYCLE + MISSING_FIELD 双重阻断', () => {
    const table = {
      service: 'svc-x',
      versionLabel: 'v1',
      versionOrdinal: 1,
      fields: [
        { path: 'id', type: 'string', role: 'identifier', required: true },
        {
          path: 'onlyWhen',
          type: 'string',
          role: 'info',
          required: true,
          defaults: [{ when: { field: 'missingDep', equals: 'x' }, value: 'y' }]
        }
      ]
    };
    const result = restoreOne({ id: 'a' }, table, 'test');
    assert.equal(result.record.onlyWhen, undefined);
    assert.ok(result.findings.some((f) => f.code === ReasonCode.DEFAULT_CYCLE && f.location === 'onlyWhen'));
    assert.ok(result.findings.some((f) => f.code === ReasonCode.MISSING_FIELD && f.location === 'onlyWhen'));
    assert.equal(result.appliedDefaults.length, 0);
  });

  it('默认值依赖自引用形成循环 -> DEFAULT_CYCLE', () => {
    const table = {
      service: 'svc-y',
      versionLabel: 'v1',
      versionOrdinal: 1,
      fields: [
        { path: 'id', type: 'string', role: 'identifier', required: true },
        { path: 'a', type: 'string', role: 'info', defaults: [{ when: { field: 'a', equals: 'x' }, value: 'a1' }] }
      ]
    };
    const result = restoreOne({ id: 'a' }, table, 'test');
    assert.ok(result.findings.some((f) => f.code === ReasonCode.DEFAULT_CYCLE));
  });

  it('必填字段缺失且无默认值 -> MISSING_FIELD', () => {
    const result = restoreOne({ id: 'x' }, approvalV2, 'test');
    assert.ok(result.findings.some((f) => f.code === ReasonCode.MISSING_FIELD));
  });

  it('嵌套未声明字段 -> UNDECLARED_FIELD，失败关闭', () => {
    const result = restoreOne(
      { id: 'x', action: 'read', target: { resourceId: 'r', region: 'z', unexpected: 1 } },
      approvalV2,
      'test'
    );
    const finding = result.findings.find((f) => f.code === ReasonCode.UNDECLARED_FIELD);
    assert.equal(finding.location, 'target.unexpected');
  });
});

describe('跨版本映射与范围收窄', () => {
  it('v3 安全词汇映射到 v2：AES-256-GCM -> AES256', () => {
    const encField = executionV3.fields.find((f) => f.path === 'encryption');
    const result = mapValueAcrossVersions('AES-256-GCM', encField, approvalV2);
    assert.deepEqual(result, { ok: true, value: 'AES256' });
  });

  it('映射表缺项 -> ok:false 并返回坏值；安全字段走 SECURITY_UNMAPPED', () => {
    const encField = executionV3.fields.find((f) => f.path === 'encryption');
    const broken = { ...encField, mappings: [{ targetService: approvalV2.service, targetVersion: 'v2', values: { DISABLED: 'NONE' } }] };
    const result = mapValueAcrossVersions('AES-256-GCM', broken, approvalV2);
    assert.equal(result.ok, false);
    assert.equal(result.badValue, 'AES-256-GCM');

    const mapped = mapRecordToVersion(
      { id: 'x', encryption: 'AES-256-GCM' },
      { ...executionV3, fields: executionV3.fields.map((f) => (f.path === 'encryption' ? broken : f)) },
      approvalV2,
      'execution-parse'
    );
    assert.equal(mapped.findings[0].code, ReasonCode.SECURITY_UNMAPPED);
  });

  it('set 逐元素映射，任一坏值整体失败', () => {
    const permField = executionV3.fields.find((f) => f.path === 'permissions');
    const broken = { ...permField, mappings: [{ targetService: approvalV2.service, targetVersion: 'v2', values: { read: 'read', write: 'write' } }] };
    const result = mapValueAcrossVersions(['read', 'admin'], broken, approvalV2);
    assert.equal(result.ok, false);
    assert.equal(result.badValue, 'admin');
  });

  it('范围子集：子集允许、超集拒绝；rank 标量只允许数值不增长', () => {
    const setField = approvalV2.fields.find((f) => f.path === 'permissions');
    assert.equal(isScopeNarrower(['read', 'write'], ['read'], setField), true);
    assert.equal(isScopeNarrower(['read'], ['read', 'write'], setField), false);

    const rankField = { path: 'level', type: 'enum', role: FieldRole.SCOPE, rank: { low: 1, high: 2 } };
    assert.equal(isScopeNarrower('high', 'low', rankField), true);
    assert.equal(isScopeNarrower('low', 'high', rankField), false);
    assert.equal(isScopeNarrower('high', 'high', rankField), true);

    const plainField = { path: 'mode', type: 'string', role: FieldRole.SCOPE };
    assert.equal(isScopeNarrower('a', 'a', plainField), true);
    assert.equal(isScopeNarrower('a', 'b', plainField), false);
  });

  it('无映射定义时原值透传（同词汇表不做转换）', () => {
    const result = mapRecordToVersion({ id: 'x', action: 'read' }, approvalV2, approvalV1, 'test');
    assert.equal(result.record.action, 'read');
    assert.equal(result.findings.length, 0);
  });
});
