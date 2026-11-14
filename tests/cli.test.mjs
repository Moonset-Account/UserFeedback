import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const appPath = path.join(root, 'app.mjs');
const fixture = path.join(root, 'fixtures', 'sample-batch.json');

function runCli(args) {
  return spawnSync(process.execPath, [appPath, ...args], { encoding: 'utf8' });
}

test('cli-sample-batch: 样例批次覆盖四类验收场景且退出码为 2（存在边界分歧）', () => {
  const res = runCli([fixture, '--json']);
  assert.equal(res.status, 2, `stderr: ${res.stderr}`);
  const report = JSON.parse(res.stdout);
  const s = report.summary;
  // 同一连接连续多条正常记录（conn-0001 三条）
  assert.equal(s.normal_forward, 3);
  // 两端都正确结束：conn-0001 最后一条两端 connection_closed=true
  const cleanClose = report.findings.find(
    (f) => f.connection_id === 'conn-0001' && f.range[0] === 260,
  );
  assert.deepEqual(cleanClose.connection_closed, { gateway: true, app: true });
  // 网关已拒绝但关闭状态缺失（conn-0002）
  const rejected = report.findings.find((f) => f.connection_id === 'conn-0002');
  assert.equal(rejected.case, 'gateway_reject');
  assert.equal(rejected.close_status, 'missing_close_evidence');
  assert.ok(rejected.pending_config_checks.length > 0);
  // 同一条消息边界不一致（conn-0003: [0,100) vs [0,64)）
  const diverged = report.findings.find((f) => f.connection_id === 'conn-0003');
  assert.equal(diverged.case, 'boundary_divergence');
  assert.equal(diverged.divergence, 'range_mismatch');
  assert.deepEqual(diverged.gateway_range, [0, 100]);
  assert.deepEqual(diverged.app_range, [0, 64]);
  // 记录不足（conn-0004 仅网关端）
  const insufficient = report.findings.find((f) => f.connection_id === 'conn-0004');
  assert.equal(insufficient.case, 'insufficient_records');
  assert.equal(insufficient.missing, 'missing_app_record');
});

test('cli-text-report: 文本报告包含连接、记录编号与配置版本', () => {
  const res = runCli([fixture]);
  assert.equal(res.status, 2, `stderr: ${res.stderr}`);
  assert.ok(res.stdout.includes('conn-0003'));
  assert.ok(res.stdout.includes('gw-conn3-001'));
  assert.ok(res.stdout.includes('app-conn3-001'));
  assert.ok(res.stdout.includes('gw-v2.4.0'));
  assert.ok(res.stdout.includes('app-v5.0.2'));
  assert.ok(res.stdout.includes('待检查配置项'));
});

test('cli-reimport-stable: 重复导入同一批记录两次运行输出完全一致', () => {
  const first = runCli([fixture, '--json']);
  const second = runCli([fixture, '--json']);
  assert.equal(first.status, second.status);
  assert.equal(first.stdout, second.stdout);
});

test('cli-clean-batch-exit-0: 无分歧无非法记录时退出码为 0', () => {
  const cleanFixture = path.join(root, 'fixtures', 'clean-batch.json');
  const res = runCli([cleanFixture, '--json']);
  assert.equal(res.status, 0, `stderr: ${res.stderr}`);
  const report = JSON.parse(res.stdout);
  assert.equal(report.summary.boundary_divergence, 0);
  assert.ok(report.summary.normal_forward >= 1);
});

test('cli-usage-error: 缺少参数退出码为 1', () => {
  const res = runCli([]);
  assert.equal(res.status, 1);
  assert.ok(res.stderr.includes('usage'));
});
