// CLI 端到端测试：以子进程运行 app.mjs，校验 JSON 输出、文件写出与退出码。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const app = path.join(root, 'app.mjs');
const dirA = path.join(root, 'fixtures', 'snapshot-a');
const dirB = path.join(root, 'fixtures', 'snapshot-b');

function runCli(args) {
  return spawnSync(process.execPath, [app, ...args], { encoding: 'utf8', cwd: root });
}

test('TT-101 双快照模式输出 CI 可读 JSON，findings 含受影响入口/加载路径/修复点', () => {
  const res = runCli(['--snapshot-a', dirA, '--snapshot-b', dirB]);
  assert.equal(res.status, 0, `stderr: ${res.stderr}`);
  const report = JSON.parse(res.stdout);
  assert.equal(report.tool, 'trusted-types-coverage-audit');
  assert.equal(report.schemaVersion, 1);
  assert.equal(report.snapshots.base, 'snapshot-a');
  assert.equal(report.snapshots.current, 'snapshot-b');
  assert.equal(report.summary.entries, 3);
  assert.equal(report.summary.sinkEvaluations, 12);
  assert.equal(report.summary.covered, 5);
  assert.ok(report.summary.findings >= 8, `findings 数: ${report.summary.findings}`);
  for (const f of report.findings) {
    assert.ok(f.id && f.category && f.severity, `finding 缺字段: ${JSON.stringify(f)}`);
    assert.ok(Array.isArray(f.entries) && f.entries.length >= 1);
    assert.ok(Array.isArray(f.loadPaths) && f.loadPaths.length >= 1);
    assert.ok(typeof f.fixPoint === 'string' && f.fixPoint.length > 0);
  }
  const categories = new Set(report.findings.map((f) => f.category));
  for (const expected of [
    'policy_not_authorized',
    'type_mismatch',
    'no_policy',
    'insufficient_info',
    'shared_module_inconsistent',
    'new_dynamic_sink_uncovered',
  ]) {
    assert.ok(categories.has(expected), `缺少类别 ${expected}`);
  }
  assert.ok(report.diff.newDynamicSinks.some((s) => s.sinkId === 's8' && !s.covered));
  assert.ok(report.diff.sharedModules.some((m) => m.module === 'ui-core' && m.resolved));
});

test('TT-102 单快照模式: 快照 A 检出 report_only，diff 为 null', () => {
  const res = runCli(['--snapshot', dirA]);
  assert.equal(res.status, 0, `stderr: ${res.stderr}`);
  const report = JSON.parse(res.stdout);
  assert.equal(report.diff, null);
  assert.equal(report.summary.entries, 2);
  assert.ok(report.summary.byCategory.report_only >= 4);
});

test('TT-103 --out 写入报告文件且内容与 stdout 模式一致', () => {
  const tmp = mkdtempSync(path.join(tmpdir(), 'tt-audit-'));
  const out = path.join(tmp, 'report.json');
  const res = runCli(['--snapshot-a', dirA, '--snapshot-b', dirB, '--out', out]);
  assert.equal(res.status, 0, `stderr: ${res.stderr}`);
  assert.equal(res.stdout, '');
  const report = JSON.parse(readFileSync(out, 'utf8'));
  assert.equal(report.tool, 'trusted-types-coverage-audit');
  assert.ok(report.summary.findings > 0);
});

test('TT-104 --fail-on-findings 存在 findings 时退出码为 2', () => {
  const res = runCli(['--snapshot-b', dirB, '--fail-on-findings']);
  assert.equal(res.status, 2, `stdout: ${res.stdout} stderr: ${res.stderr}`);
  const report = JSON.parse(res.stdout);
  assert.ok(report.findings.length > 0);
});

test('TT-105 参数缺失或快照目录非法时退出码为 1', () => {
  const noArgs = runCli([]);
  assert.equal(noArgs.status, 1);
  assert.match(noArgs.stderr, /缺少快照目录参数/);
  const badDir = runCli(['--snapshot', path.join(root, 'fixtures', 'no-such-dir')]);
  assert.equal(badDir.status, 1);
  assert.match(badDir.stderr, /读取或分析快照失败/);
  const onlyA = runCli(['--snapshot-a', dirA]);
  assert.equal(onlyA.status, 1);
});
