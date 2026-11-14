import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import { validateAlerts, validateExemptions, parseAuditTime } from '../src/parse.mjs';
import { buildReport, evaluateAlert, DECISION, REASON } from '../src/audit.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(here, '..', 'fixtures');

const AUDIT_AT = '2026-11-14T09:30:00Z';

async function loadFixtureSet() {
  const [alertRaw, exRaw] = await Promise.all([
    readFile(path.join(FIXTURES, 'alerts.json'), 'utf8'),
    readFile(path.join(FIXTURES, 'exemptions.json'), 'utf8'),
  ]);
  const alerts = validateAlerts(JSON.parse(alertRaw));
  const exemptions = validateExemptions(JSON.parse(exRaw));
  return { alerts, exemptions };
}

function resultById(report, alertId) {
  const found = report.results_flat.find((r) => r.alert_id === alertId);
  assert.ok(found, `报告中应包含告警 ${alertId}`);
  return found;
}

// 逐项样例/边界断言表：稳定 case_id -> 输入标识与期望输出。
const CASE_TABLE = [
  {
    case_id: 'CASE-01-valid-exemption',
    alert_id: 'ALRT-001',
    expect: {
      decision: DECISION.CARRIED,
      reason_code: REASON.EXEMPTION_VALID,
      exemption_id: 'EXM-001',
    },
  },
  {
    case_id: 'CASE-02-same-vuln-different-location',
    alert_id: 'ALRT-002',
    expect: {
      decision: DECISION.REOPEN,
      reason_code: REASON.LOCATION_MISMATCH,
      exemption_id: 'EXM-001',
    },
  },
  {
    case_id: 'CASE-03-rule-version-changed',
    alert_id: 'ALRT-003',
    expect: {
      decision: DECISION.MANUAL,
      reason_code: REASON.RULE_VERSION_CHANGED,
      exemption_id: 'EXM-003',
    },
  },
  {
    case_id: 'CASE-04-evidence-withdrawn',
    alert_id: 'ALRT-004',
    expect: {
      decision: DECISION.MANUAL,
      reason_code: REASON.EVIDENCE_WITHDRAWN,
      exemption_id: 'EXM-004',
    },
  },
  {
    case_id: 'CASE-05-audit-time-equals-expiry',
    alert_id: 'ALRT-008',
    expect: {
      decision: DECISION.CARRIED,
      reason_code: REASON.EXEMPTION_VALID,
      exemption_id: 'EXM-008',
    },
  },
  {
    case_id: 'CASE-06-exemption-expired',
    alert_id: 'ALRT-005',
    expect: {
      decision: DECISION.MANUAL,
      reason_code: REASON.EXEMPTION_EXPIRED,
      exemption_id: 'EXM-005',
    },
  },
  {
    case_id: 'CASE-07-evidence-revision-changed',
    alert_id: 'ALRT-006',
    expect: {
      decision: DECISION.REOPEN,
      reason_code: REASON.EVIDENCE_REVISION_CHANGED,
      exemption_id: 'EXM-006',
    },
  },
  {
    case_id: 'CASE-08-rule-version-reconfirmed',
    alert_id: 'ALRT-007',
    expect: {
      decision: DECISION.CARRIED,
      reason_code: REASON.EXEMPTION_VALID,
      exemption_id: 'EXM-007',
    },
  },
  {
    case_id: 'CASE-09-no-exemption-on-record',
    alert_id: 'ALRT-009',
    expect: {
      decision: DECISION.REOPEN,
      reason_code: REASON.NO_MATCHING_EXEMPTION,
      exemption_id: null,
    },
  },
];

test('豁免审计：基于合成夹具的逐项样例断言（case_id 稳定）', async (t) => {
  const { alerts, exemptions } = await loadFixtureSet();
  const report = buildReport(alerts, exemptions, parseAuditTime(AUDIT_AT), AUDIT_AT);

  for (const row of CASE_TABLE) {
    await t.test(row.case_id, () => {
      const result = resultById(report, row.alert_id);

      assert.equal(
        result.decision,
        row.expect.decision,
        `${row.case_id}: 决策分类应为 ${row.expect.decision}，实际 ${result.decision}`,
      );
      assert.equal(
        result.reason_code,
        row.expect.reason_code,
        `${row.case_id}: 原因码应为 ${row.expect.reason_code}`,
      );
      assert.ok(result.reason.length > 0, `${row.case_id}: 必须给出中文决策依据`);

      if (row.expect.exemption_id === null) {
        assert.deepEqual(result.matched_exemptions, [], `${row.case_id}: 不应匹配任何豁免`);
      } else {
        assert.equal(
          result.matched_exemptions[0]?.exemption_id,
          row.expect.exemption_id,
          `${row.case_id}: 应匹配豁免 ${row.expect.exemption_id}`,
        );
      }
    });
  }
});

test('CASE-02 细节：漏洞类型相同但位置不同，旧豁免不得隐藏新位置告警', async () => {
  const { alerts, exemptions } = await loadFixtureSet();
  const alert = alerts.find((a) => a.alert_id === 'ALRT-002');
  const result = evaluateAlert(alert, exemptions, parseAuditTime(AUDIT_AT));

  assert.equal(result.decision, DECISION.REOPEN);
  assert.equal(result.reason_code, REASON.LOCATION_MISMATCH);
  assert.equal(result.scope_check.same_project, true);
  assert.equal(result.scope_check.same_rule, true);
  assert.equal(result.scope_check.same_location, false);
  assert.equal(result.vuln_type, 'sql_injection');
  assert.equal(result.matched_exemptions[0].location_id, 'src/db/orders.ts:118');
  assert.notEqual(result.location_id, result.matched_exemptions[0].location_id);
});

test('CASE-03 细节：规则版本变化且未重新确认时显式报告失效原因', async () => {
  const { alerts, exemptions } = await loadFixtureSet();
  const alert = alerts.find((a) => a.alert_id === 'ALRT-003');
  const result = evaluateAlert(alert, exemptions, parseAuditTime(AUDIT_AT));

  assert.equal(result.decision, DECISION.MANUAL);
  assert.equal(result.reason_code, REASON.RULE_VERSION_CHANGED);
  assert.deepEqual(result.rule_version_check, {
    exemption_rule_version: '2026.09',
    alert_rule_version: '2026.11',
    reconfirmed_rule_version: null,
    reconfirmed: false,
  });
});

test('CASE-04 细节：审查证据被撤回，豁免失去事实基础转人工', async () => {
  const { alerts, exemptions } = await loadFixtureSet();
  const alert = alerts.find((a) => a.alert_id === 'ALRT-004');
  const result = evaluateAlert(alert, exemptions, parseAuditTime(AUDIT_AT));

  assert.equal(result.decision, DECISION.MANUAL);
  assert.equal(result.reason_code, REASON.EVIDENCE_WITHDRAWN);
  assert.equal(result.evidence_check.status, 'withdrawn');
  assert.equal(result.evidence_status, 'withdrawn');
});

test('CASE-05 边界：核验时间恰好等于 valid_until，豁免仍然有效', async () => {
  const { alerts, exemptions } = await loadFixtureSet();
  const at = '2026-11-14T09:30:00Z';
  const atMs = parseAuditTime(at);
  const alert = alerts.find((a) => a.alert_id === 'ALRT-008');
  const result = evaluateAlert(alert, exemptions, atMs);

  assert.equal(result.decision, DECISION.CARRIED);
  assert.equal(result.time_check.at_valid_until_boundary, true);

  // 晚一毫秒即过期：边界语义必须是闭区间。
  const expired = evaluateAlert(alert, exemptions, atMs + 1);
  assert.equal(expired.decision, DECISION.MANUAL);
  assert.equal(expired.reason_code, REASON.EXEMPTION_EXPIRED);
});

test('CASE-07 细节：证据修订号变化即重新打开，即使其他四要素一致', async () => {
  const { alerts, exemptions } = await loadFixtureSet();
  const alert = alerts.find((a) => a.alert_id === 'ALRT-006');
  const result = evaluateAlert(alert, exemptions, parseAuditTime(AUDIT_AT));

  assert.equal(result.decision, DECISION.REOPEN);
  assert.deepEqual(result.evidence_check, {
    exemption_evidence_revision: 'rev-2f00',
    alert_evidence_revision: 'rev-3e77',
  });
});

test('CASE-08 对照：规则版本变化但已按新版本重新确认，豁免继续沿用', async () => {
  const { alerts, exemptions } = await loadFixtureSet();
  const alert = alerts.find((a) => a.alert_id === 'ALRT-007');
  const result = evaluateAlert(alert, exemptions, parseAuditTime(AUDIT_AT));

  assert.equal(result.decision, DECISION.CARRIED);
  assert.equal(result.matched_exemptions[0].reconfirmed_rule_version, '2026.10');
});

test('三类结果桶完整且汇总计数与明细一致', async () => {
  const { alerts, exemptions } = await loadFixtureSet();
  const report = buildReport(alerts, exemptions, parseAuditTime(AUDIT_AT), AUDIT_AT);

  assert.deepEqual(report.summary, { total: 9, carried: 3, reopen: 3, manual: 3 });
  assert.equal(
    report.results.carried.length + report.results.reopen.length + report.results.manual.length,
    report.summary.total,
  );
  assert.ok(report.results.carried.every((r) => r.decision === DECISION.CARRIED));
  assert.ok(report.results.reopen.every((r) => r.decision === DECISION.REOPEN));
  assert.ok(report.results.manual.every((r) => r.decision === DECISION.MANUAL));
});

test('稳定性：同一输入两次生成的报告字节级一致，且与输入顺序无关', async () => {
  const { alerts, exemptions } = await loadFixtureSet();
  const atMs = parseAuditTime(AUDIT_AT);

  const reportA = buildReport(alerts, exemptions, atMs, AUDIT_AT);
  const reportB = buildReport(alerts, exemptions, atMs, AUDIT_AT);
  assert.equal(JSON.stringify(reportA), JSON.stringify(reportB));

  const shuffled = [...alerts].reverse();
  const reportC = buildReport(shuffled, exemptions, atMs, AUDIT_AT);
  assert.equal(JSON.stringify(reportA), JSON.stringify(reportC));
  const ids = reportC.results_flat.map((r) => r.alert_id);
  assert.deepEqual(ids, [...ids].sort());
});

test('边界补充：豁免尚未到生效时间转人工，不提前隐藏告警', () => {
  const alert = {
    alert_id: 'ALRT-FUTURE',
    project_id: 'p',
    rule_id: 'r',
    rule_version: 'v1',
    location_id: 'loc:1',
    vuln_type: 'x',
    evidence_revision: 'e1',
    evidence_status: 'active',
  };
  const exemption = {
    exemption_id: 'EXM-FUTURE',
    project_id: 'p',
    rule_id: 'r',
    rule_version: 'v1',
    location_id: 'loc:1',
    evidence_revision: 'e1',
    valid_from: '2026-12-01T00:00:00Z',
    valid_until: '2026-12-31T00:00:00Z',
    valid_from_ms: Date.parse('2026-12-01T00:00:00Z'),
    valid_until_ms: Date.parse('2026-12-31T00:00:00Z'),
    reconfirmed_rule_version: null,
    rationale: '',
    granted_by: '',
  };
  const result = evaluateAlert(alert, [exemption], parseAuditTime(AUDIT_AT));
  assert.equal(result.decision, DECISION.MANUAL);
  assert.equal(result.reason_code, REASON.EXEMPTION_NOT_YET_VALID);
  assert.equal(result.time_check.audit_before_valid_from, true);
});

test('冲突补充：同一精确范围存在两条豁免时转人工处理', () => {
  const alert = {
    alert_id: 'ALRT-DUP',
    project_id: 'p',
    rule_id: 'r',
    rule_version: 'v1',
    location_id: 'loc:2',
    vuln_type: 'x',
    evidence_revision: 'e1',
    evidence_status: 'active',
  };
  const mk = (id) => ({
    exemption_id: id,
    project_id: 'p',
    rule_id: 'r',
    rule_version: 'v1',
    location_id: 'loc:2',
    evidence_revision: 'e1',
    valid_from: '2026-10-01T00:00:00Z',
    valid_until: '2026-12-31T00:00:00Z',
    valid_from_ms: Date.parse('2026-10-01T00:00:00Z'),
    valid_until_ms: Date.parse('2026-12-31T00:00:00Z'),
    reconfirmed_rule_version: null,
    rationale: '',
    granted_by: '',
  });
  const result = evaluateAlert(alert, [mk('EXM-A'), mk('EXM-B')], parseAuditTime(AUDIT_AT));
  assert.equal(result.decision, DECISION.MANUAL);
  assert.equal(result.reason_code, REASON.CONFLICTING_EXEMPTIONS);
  assert.deepEqual(
    result.matched_exemptions.map((e) => e.exemption_id),
    ['EXM-A', 'EXM-B'],
  );
});

test('输入校验：缺字段、坏时间、重复 ID、非法证据状态均被拒绝', () => {
  assert.throws(
    () => validateAlerts([{ alert_id: 'a' }]),
    /字段 "project_id" 必须是非空字符串/,
  );
  assert.throws(
    () =>
      validateAlerts([
        {
          alert_id: 'a',
          project_id: 'p',
          rule_id: 'r',
          rule_version: 'v',
          location_id: 'l',
          vuln_type: 't',
          evidence_revision: 'e',
        },
        {
          alert_id: 'a',
          project_id: 'p',
          rule_id: 'r',
          rule_version: 'v',
          location_id: 'l',
          vuln_type: 't',
          evidence_revision: 'e',
        },
      ]),
    /alert_id 重复/,
  );
  assert.throws(
    () =>
      validateAlerts([
        {
          alert_id: 'a',
          project_id: 'p',
          rule_id: 'r',
          rule_version: 'v',
          location_id: 'l',
          vuln_type: 't',
          evidence_revision: 'e',
          evidence_status: 'deleted',
        },
      ]),
    /evidence_status 只允许 active 或 withdrawn/,
  );
  assert.throws(() => parseAuditTime('not-a-time'), /无法解析为时间/);
  assert.throws(
    () =>
      validateExemptions([
        {
          exemption_id: 'x',
          project_id: 'p',
          rule_id: 'r',
          rule_version: 'v',
          location_id: 'l',
          evidence_revision: 'e',
          valid_from: '2026-12-31T00:00:00Z',
          valid_until: '2026-12-01T00:00:00Z',
        },
      ]),
    /valid_from 晚于 valid_until/,
  );
});
