import { test } from 'node:test';
import assert from 'node:assert/strict';
import { analyzeBatch, renderText } from '../lib/analyze.mjs';

let seq = 0;
function rec(overrides) {
  seq += 1;
  return {
    record_id: `r-${seq}`,
    side: 'gateway',
    config_version: 'v1',
    connection_id: 'conn-x',
    start_offset: 0,
    end_offset: 100,
    delimitation: 'ok',
    reject_reason: null,
    connection_closed: null,
    ...overrides,
  };
}

function pair(connId, start, end, extra = {}) {
  return [
    rec({ record_id: `gw-${connId}-${start}`, side: 'gateway', connection_id: connId, start_offset: start, end_offset: end, config_version: 'gw-v1', ...extra.gateway }),
    rec({ record_id: `app-${connId}-${start}`, side: 'app', connection_id: connId, start_offset: start, end_offset: end, config_version: 'app-v1', ...extra.app }),
  ];
}

test('case-consecutive-normal: 同一连接连续多条正常记录全部判为 normal_forward', () => {
  const records = [
    ...pair('conn-a', 0, 100),
    ...pair('conn-a', 100, 250),
    ...pair('conn-a', 250, 400),
  ];
  const report = analyzeBatch({ batch_id: 't1', records });
  assert.equal(report.summary.normal_forward, 3);
  assert.equal(report.summary.boundary_divergence, 0);
  assert.equal(report.summary.gateway_reject, 0);
  assert.equal(report.summary.insufficient_records, 0);
  assert.equal(report.summary.connections, 1);
  const ranges = report.findings.map((f) => f.range);
  assert.deepEqual(ranges, [[0, 100], [100, 250], [250, 400]]);
  for (const f of report.findings) {
    assert.equal(f.connection_id, 'conn-a');
    assert.equal(f.config_versions.gateway, 'gw-v1');
    assert.equal(f.config_versions.app, 'app-v1');
    assert.ok(f.gateway_record_id && f.app_record_id, '结论须定位到两端记录编号');
  }
});

test('case-clean-close: 两端都正确结束（最后一条消息两端 connection_closed=true）', () => {
  const records = [
    ...pair('conn-b', 0, 100),
    ...pair('conn-b', 100, 200, {
      gateway: { connection_closed: true },
      app: { connection_closed: true },
    }),
  ];
  const report = analyzeBatch({ records });
  assert.equal(report.summary.normal_forward, 2);
  const last = report.findings.find((f) => f.range[0] === 100);
  assert.equal(last.case, 'normal_forward');
  assert.deepEqual(last.connection_closed, { gateway: true, app: true });
});

test('case-reject-missing-close-evidence: 网关已拒绝但关闭状态缺失', () => {
  const records = [
    rec({
      record_id: 'gw-c-1',
      side: 'gateway',
      connection_id: 'conn-c',
      delimitation: 'rejected',
      reject_reason: 'ambiguous Content-Length',
      connection_closed: null,
      config_version: 'gw-v9',
    }),
  ];
  const report = analyzeBatch({ records });
  assert.equal(report.summary.gateway_reject, 1);
  assert.equal(report.summary.close_status.missing_close_evidence, 1);
  const f = report.findings[0];
  assert.equal(f.case, 'gateway_reject');
  assert.equal(f.close_status, 'missing_close_evidence');
  assert.equal(f.connection_id, 'conn-c');
  assert.equal(f.gateway_record_id, 'gw-c-1');
  assert.equal(f.config_versions.gateway, 'gw-v9');
  assert.ok(f.pending_config_checks.length > 0, '须给出待检查配置项');
  assert.ok(f.pending_config_checks.includes('gateway.logging.record_connection_close'));
});

test('case-reject-closed-vs-reused: 网关拒绝后区分已关闭与可能复用', () => {
  const records = [
    rec({
      record_id: 'gw-d-1',
      connection_id: 'conn-d',
      delimitation: 'rejected',
      reject_reason: 'bad chunk size',
      connection_closed: true,
    }),
    rec({
      record_id: 'gw-e-1',
      connection_id: 'conn-e',
      delimitation: 'rejected',
      reject_reason: 'bad chunk size',
      connection_closed: false,
    }),
  ];
  const report = analyzeBatch({ records });
  assert.equal(report.summary.close_status.connection_closed, 1);
  assert.equal(report.summary.close_status.possibly_reused, 1);
  const closed = report.findings.find((f) => f.connection_id === 'conn-d');
  const reused = report.findings.find((f) => f.connection_id === 'conn-e');
  assert.equal(closed.close_status, 'connection_closed');
  assert.equal(reused.close_status, 'possibly_reused');
  assert.ok(reused.pending_config_checks.includes('gateway.connection.reuse_after_error_response'));
});

test('case-boundary-mismatch: 同一条消息两端边界不一致判为 boundary_divergence(range_mismatch)', () => {
  const records = [
    rec({ record_id: 'gw-f-1', side: 'gateway', connection_id: 'conn-f', start_offset: 0, end_offset: 100 }),
    rec({ record_id: 'app-f-1', side: 'app', connection_id: 'conn-f', start_offset: 0, end_offset: 64 }),
  ];
  const report = analyzeBatch({ records });
  assert.equal(report.summary.boundary_divergence, 1);
  assert.equal(report.summary.normal_forward, 0);
  const f = report.findings[0];
  assert.equal(f.case, 'boundary_divergence');
  assert.equal(f.divergence, 'range_mismatch');
  assert.deepEqual(f.gateway_range, [0, 100]);
  assert.deepEqual(f.app_range, [0, 64]);
  assert.equal(f.gateway_record_id, 'gw-f-1');
  assert.equal(f.app_record_id, 'app-f-1');
  assert.ok(f.pending_config_checks.length > 0);
});

test('case-decision-mismatch: 同一区间一端放行一端拒绝判为 boundary_divergence(decision_mismatch)', () => {
  const records = [
    rec({ record_id: 'gw-g-1', side: 'gateway', connection_id: 'conn-g' }),
    rec({
      record_id: 'app-g-1',
      side: 'app',
      connection_id: 'conn-g',
      delimitation: 'rejected',
      reject_reason: 'invalid transfer coding',
    }),
  ];
  const report = analyzeBatch({ records });
  assert.equal(report.summary.boundary_divergence, 1);
  const f = report.findings[0];
  assert.equal(f.divergence, 'decision_mismatch');
  assert.equal(f.gateway_delimitation, 'ok');
  assert.equal(f.app_delimitation, 'rejected');
});

test('case-insufficient-records: 仅一端有记录判为 insufficient_records', () => {
  const records = [
    rec({ record_id: 'gw-h-1', side: 'gateway', connection_id: 'conn-h' }),
    rec({ record_id: 'app-i-1', side: 'app', connection_id: 'conn-i' }),
  ];
  const report = analyzeBatch({ records });
  assert.equal(report.summary.insufficient_records, 2);
  const gwOnly = report.findings.find((f) => f.connection_id === 'conn-h');
  const appOnly = report.findings.find((f) => f.connection_id === 'conn-i');
  assert.equal(gwOnly.missing, 'missing_app_record');
  assert.equal(appOnly.missing, 'missing_gateway_record');
  assert.ok(gwOnly.pending_config_checks.length > 0);
});

test('case-reimport-stable: 重复导入同一批记录结论稳定（完全重复被忽略）', () => {
  const batch = [
    ...pair('conn-j', 0, 100),
    ...pair('conn-j', 100, 200),
    rec({
      record_id: 'gw-j-rej',
      connection_id: 'conn-j',
      start_offset: 200,
      end_offset: 260,
      delimitation: 'rejected',
      reject_reason: 'smuggling attempt',
      connection_closed: null,
    }),
  ];
  const once = analyzeBatch({ batch_id: 't-reimport', records: batch });
  const twice = analyzeBatch({ batch_id: 't-reimport', records: [...batch, ...batch] });
  assert.deepEqual(twice.findings, once.findings);
  assert.equal(twice.summary.duplicates_ignored, batch.length);
  assert.equal(twice.summary.total_input_records, batch.length * 2);
  assert.equal(twice.summary.valid_records, once.summary.valid_records);
  // 逐条结论稳定：分类汇总与文本报告中的结论行一致（重复计数行除外）
  for (const key of ['normal_forward', 'gateway_reject', 'boundary_divergence', 'insufficient_records']) {
    assert.equal(twice.summary[key], once.summary[key]);
  }
  const findingLines = (r) => renderText(r).split('\n').filter((l) => l.startsWith('- ['));
  assert.deepEqual(findingLines(twice), findingLines(once));
});

test('case-order-independent: 输入顺序打乱后结论逐条一致', () => {
  const records = [
    ...pair('conn-k', 0, 100),
    ...pair('conn-k', 100, 200),
    ...pair('conn-z', 0, 50),
  ];
  const shuffled = [...records].reverse();
  const a = analyzeBatch({ records });
  const b = analyzeBatch({ records: shuffled });
  assert.deepEqual(b.findings, a.findings);
  assert.deepEqual(b.summary, a.summary);
});

test('case-conflicting-record-id: 同 record_id 不同内容记为冲突且保留首条', () => {
  const r1 = rec({ record_id: 'dup-1', connection_id: 'conn-l' });
  const r2 = { ...r1, end_offset: 150 };
  const report = analyzeBatch({ records: [r1, r2] });
  assert.equal(report.summary.conflicts, 1);
  assert.equal(report.conflicts[0].record_id, 'dup-1');
  assert.equal(report.summary.valid_records, 1);
});

test('case-invalid-records: 非法记录被剔除并逐条列出错误', () => {
  const good = rec({ record_id: 'ok-1', connection_id: 'conn-m' });
  const bad1 = rec({ record_id: 'bad-1', connection_id: 'conn-m', end_offset: 0 });
  const bad2 = rec({ record_id: 'bad-2', connection_id: 'conn-m', delimitation: 'rejected', reject_reason: null });
  const bad3 = rec({ record_id: 'bad-3', connection_id: 'conn-m', connection_closed: 'yes' });
  const report = analyzeBatch({ records: [good, bad1, bad2, bad3] });
  assert.equal(report.summary.invalid_records, 3);
  assert.equal(report.summary.valid_records, 1);
  const ids = report.invalid_records.map((r) => r.record_id).sort();
  assert.deepEqual(ids, ['bad-1', 'bad-2', 'bad-3']);
  for (const inv of report.invalid_records) {
    assert.ok(inv.errors.length > 0);
  }
});

test('case-report-locates: 报告可定位到连接、记录编号与配置版本', () => {
  const records = [
    rec({
      record_id: 'gw-n-1',
      side: 'gateway',
      connection_id: 'conn-n',
      start_offset: 0,
      end_offset: 90,
      config_version: 'gw-v2.4.0',
    }),
    rec({
      record_id: 'app-n-1',
      side: 'app',
      connection_id: 'conn-n',
      start_offset: 0,
      end_offset: 45,
      config_version: 'app-v5.0.2',
    }),
  ];
  const report = analyzeBatch({ records });
  const text = renderText(report);
  assert.ok(text.includes('conn-n'), '文本报告包含连接标识');
  assert.ok(text.includes('gw-n-1') && text.includes('app-n-1'), '文本报告包含记录编号');
  assert.ok(text.includes('gw-v2.4.0') && text.includes('app-v5.0.2'), '文本报告包含配置版本');
  assert.ok(text.includes('boundary_divergence'));
});
