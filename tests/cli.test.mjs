import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(here, '..');
const APP = path.join(ROOT, 'app.mjs');
const ALERTS = path.join(ROOT, 'fixtures', 'alerts.json');
const EXEMPTIONS = path.join(ROOT, 'fixtures', 'exemptions.json');
const AT = '2026-11-14T09:30:00Z';

function runCli(args) {
  return spawnSync(process.execPath, [APP, ...args], {
    encoding: 'utf8',
    cwd: ROOT,
  });
}

test('CLI-CASE-01：对合成夹具生成完整三类报告，退出码为 0', () => {
  const proc = runCli(['--alerts', ALERTS, '--exemptions', EXEMPTIONS, '--at', AT]);
  assert.equal(proc.status, 0, `stderr: ${proc.stderr}`);

  const report = JSON.parse(proc.stdout);
  assert.equal(report.report_schema, 'exemption-audit/v1');
  assert.equal(report.generated_at, AT);
  assert.deepEqual(report.summary, { total: 9, carried: 3, reopen: 3, manual: 3 });

  const flat = report.results_flat;
  const pick = (id) => flat.find((r) => r.alert_id === id);
  assert.equal(pick('ALRT-001').decision, 'carried');
  assert.equal(pick('ALRT-002').decision, 'reopen');
  assert.equal(pick('ALRT-003').decision, 'manual');
  assert.equal(pick('ALRT-003').reason_code, 'rule_version_changed_unreconfirmed');
  assert.equal(pick('ALRT-004').reason_code, 'evidence_withdrawn');
  assert.equal(pick('ALRT-008').decision, 'carried');
  assert.equal(pick('ALRT-008').time_check.at_valid_until_boundary, true);
});

test('CLI-CASE-02：同一输入重复运行，标准输出字节级稳定', () => {
  const a = runCli(['--alerts', ALERTS, '--exemptions', EXEMPTIONS, '--at', AT]);
  const b = runCli(['--alerts', ALERTS, '--exemptions', EXEMPTIONS, '--at', AT]);
  assert.equal(a.status, 0);
  assert.equal(b.status, 0);
  assert.equal(a.stdout, b.stdout);
});

test('CLI-CASE-03：--out 写文件与标准输出内容一致', () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'sec-vuln-048-'));
  const outPath = path.join(dir, 'report.json');
  const proc = runCli([
    '--alerts',
    ALERTS,
    '--exemptions',
    EXEMPTIONS,
    '--at',
    AT,
    '--out',
    outPath,
  ]);
  assert.equal(proc.status, 0, proc.stderr);
  assert.equal(proc.stdout, '');
  assert.ok(existsSync(outPath));

  const stdoutRun = runCli(['--alerts', ALERTS, '--exemptions', EXEMPTIONS, '--at', AT]);
  assert.equal(readFileSync(outPath, 'utf8'), stdoutRun.stdout);
});

test('CLI-CASE-04：缺少必填参数退出码为 2，非法输入退出码为 1', () => {
  const missing = runCli(['--alerts', ALERTS]);
  assert.equal(missing.status, 2);
  assert.match(missing.stderr, /--alerts、--exemptions、--at 均为必填项/);

  const badTime = runCli(['--alerts', ALERTS, '--exemptions', EXEMPTIONS, '--at', 'nope']);
  assert.equal(badTime.status, 1);
  assert.match(badTime.stderr, /无法解析为时间/);
});

test('CLI-CASE-05：不读取系统时钟，未传 --at 不会产生静默默认时间', () => {
  const proc = runCli(['--alerts', ALERTS, '--exemptions', EXEMPTIONS]);
  assert.notEqual(proc.status, 0);
  assert.match(proc.stderr, /--at/);
});
