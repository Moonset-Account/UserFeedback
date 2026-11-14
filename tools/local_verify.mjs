#!/usr/bin/env node
// SEC-VULN-031 本地核验入口：真实执行当前源码与测试，并校验实际执行的测试数。
// 退出码非零的情形：测试子进程异常/失败、实际执行测试数为 0、存在失败/跳过全部为 0、CLI 冒烟断言失败。
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { readFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const root = fileURLToPath(new URL('..', import.meta.url));
let failed = false;
const fail = (msg) => { failed = true; console.error(`[FAIL] ${msg}`); };
const pass = (msg) => console.log(`[OK] ${msg}`);

// 1) 真实执行测试套件（TAP 输出），保留原始输出。
console.log('=== 1/2 执行 node:test 测试套件 ===');
const t = spawnSync(process.execPath, ['--test', '--test-reporter=tap', 'tests/audit.test.mjs'], {
  cwd: root, encoding: 'utf8', timeout: 60000,
});
if (t.stdout) process.stdout.write(t.stdout);
if (t.stderr) process.stderr.write(t.stderr);
if (t.error) fail(`测试子进程异常: ${t.error.message}`);
if (t.status !== 0) fail(`测试子进程退出码 ${t.status}`);

const lines = (t.stdout ?? '').split('\n');
if (!lines.some((l) => l.startsWith('TAP version'))) {
  fail('未检测到 TAP 输出，测试可能未真正执行');
}
const okCount = lines.filter((l) => /^\s*ok \d+/.test(l)).length;
const notOkCount = lines.filter((l) => /^\s*not ok \d+/.test(l)).length;
console.log(`实际执行断言项: ok=${okCount}, not ok=${notOkCount}`);
if (okCount < 1) fail('实际执行的测试断言数为 0');
if (notOkCount > 0) fail(`存在 ${notOkCount} 项失败断言`);
if (t.status === 0 && notOkCount === 0 && okCount >= 1) pass(`测试套件通过（${okCount} 项断言）`);

// 2) CLI 冒烟：真实执行 app.mjs 当前源码，校验报告语义。
console.log('=== 2/2 执行 CLI 冒烟（fixtures 合成数据） ===');
const outDir = mkdtempSync(path.join(tmpdir(), 'sec-vuln-031-verify-'));
const outPath = path.join(outDir, 'report.json');
const cli = spawnSync(process.execPath, [
  path.join(root, 'app.mjs'),
  '--schedule', path.join(root, 'fixtures', 'schedule.json'),
  '--cache', path.join(root, 'fixtures', 'cache.json'),
  '--decisions', path.join(root, 'fixtures', 'decisions.json'),
  '--at', '6000',
  '--out', outPath,
], { cwd: root, encoding: 'utf8', timeout: 30000 });
if (cli.stdout) process.stdout.write(cli.stdout);
if (cli.stderr) process.stderr.write(cli.stderr);
if (cli.status !== 0) {
  fail(`CLI 退出码 ${cli.status}`);
} else {
  try {
    const report = JSON.parse(readFileSync(outPath, 'utf8'));
    const checks = [
      ['summary.findings_total === 9', report.summary?.findings_total === 9],
      ['summary.decisions_total === 18', report.summary?.decisions_total === 18],
      ['summary.anomalies_total === 9', report.summary?.anomalies_total === 9],
      ['trusted_sets.at === 6000', report.trusted_sets?.at === 6000],
      ['含宽限期误拒发现', report.findings?.some((f) => f.case_id === 'case_false_reject_grace' && f.category === 'false_reject' && f.attribution === 'cache_refresh')],
      ['含退役后过度信任发现', report.findings?.some((f) => f.case_id === 'case_overtrust_after_retire' && f.category === 'overtrust_retired')],
      ['含证据不足发现', report.findings?.some((f) => f.case_id === 'case_missing_snapshot' && f.category === 'insufficient_evidence' && f.attribution === null)],
      ['含跨颁发方绑定发现', report.findings?.some((f) => f.case_id === 'case_xiss_overtrust_b' && f.attribution === 'issuer_binding')],
      ['发现可定位（实例/缓存版本/时间）', report.findings?.every((f) => typeof f.instance_id === 'string' && typeof f.cache_version === 'number' && typeof f.decided_at === 'number')],
    ];
    for (const [name, ok] of checks) {
      if (ok) pass(`CLI 报告断言: ${name}`);
      else fail(`CLI 报告断言失败: ${name}`);
    }
  } catch (e) {
    fail(`CLI 报告解析失败: ${e.message}`);
  }
}

if (failed) {
  console.error('本地核验: FAIL');
  process.exit(1);
}
console.log('本地核验: PASS');
