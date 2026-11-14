#!/usr/bin/env node
// 本地核验入口：固定 Node 路径真实执行当前源码与 node:test 套件，
// 解析 TAP 汇总行统计实际执行测试数；零测试、跳过/TODO、断言失败、
// 进程异常均以非零退出。另跑一次 CLI 冒烟核验报告与稳定性。
//
// 用法：/Users/xingyaolei/.nvm/versions/node/v24.15.0/bin/node tools/local_verify.mjs

import { spawnSync } from 'node:child_process';
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const NODE = '/Users/xingyaolei/.nvm/versions/node/v24.15.0/bin/node';
const here = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(here, '..');
const TEST_DIR = path.join(ROOT, 'tests');
const OUT_DIR = path.join(ROOT, '.verify-output');
const TAP_LOG = path.join(OUT_DIR, 'node-test.tap.log');
const CLI_REPORT = path.join(OUT_DIR, 'cli-report.json');
const SUMMARY_LOG = path.join(OUT_DIR, 'verify-summary.txt');

const HARD_BUDGET_MS = 80_000;
const lines = [];
function log(msg) {
  lines.push(msg);
  process.stdout.write(`${msg}\n`);
}
function fail(msg) {
  log(`✖ 核验失败：${msg}`);
  writeFileSync(SUMMARY_LOG, `${lines.join('\n')}\n`, 'utf8');
  process.exit(1);
}

mkdirSync(OUT_DIR, { recursive: true });

const testFiles = readdirSync(TEST_DIR)
  .filter((name) => name.endsWith('.test.mjs'))
  .sort()
  .map((name) => path.join(TEST_DIR, name));

if (testFiles.length === 0) {
  fail('tests/ 下没有发现 *.test.mjs，无法证明任何断言');
}

log(`① 使用运行时：${NODE}`);
const versionProbe = spawnSync(NODE, ['--version'], { encoding: 'utf8' });
if (versionProbe.status !== 0) {
  fail(`固定 Node 路径不可执行：${versionProbe.stderr || versionProbe.error?.message}`);
}
log(`   node ${versionProbe.stdout.trim()}`);
log(`② 发现测试文件 ${testFiles.length} 个：${testFiles.map((f) => path.relative(ROOT, f)).join(', ')}`);

const startedAt = Date.now();
const testRun = spawnSync(
  NODE,
  ['--test', '--test-reporter=tap', ...testFiles],
  {
    cwd: ROOT,
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
    timeout: HARD_BUDGET_MS,
  },
);
const elapsed = Date.now() - startedAt;
const combined = `${testRun.stdout ?? ''}\n${testRun.stderr ?? ''}`;
writeFileSync(TAP_LOG, combined, 'utf8');
log(`③ 测试执行结束，退出码=${testRun.status ?? 'null'}，耗时=${elapsed}ms，原始 TAP 已保存：${path.relative(ROOT, TAP_LOG)}`);

if (testRun.error) {
  fail(`测试进程异常：${testRun.error.message}`);
}
if (elapsed > HARD_BUDGET_MS) {
  fail(`测试耗时 ${elapsed}ms 超过 ${HARD_BUDGET_MS}ms 预算`);
}

// 解析测试汇总行。同时兼容 TAP 规范的 "# tests 26" 与 node 默认 reporter 的 "ℹ tests 26"。
// 只认真实汇总数字，不接受仅看子进程退出码。
function summaryNumber(label) {
  const re = new RegExp(`^(?:#|ℹ)\\s+${label}\\s+(\\d+)\\s*$`, 'm');
  const m = combined.match(re);
  return m ? Number(m[1]) : null;
}
const counts = {
  tests: summaryNumber('tests'),
  pass: summaryNumber('pass'),
  fail: summaryNumber('fail'),
  skipped: summaryNumber('skipped'),
  todo: summaryNumber('todo'),
};
log(`④ TAP 汇总：${JSON.stringify(counts)}`);

if (counts.tests === null || counts.pass === null || counts.fail === null) {
  fail('未能从 TAP 输出解析出 tests/pass/fail 汇总行，不能接受“退出 0”作为证据');
}
if (counts.tests < 1) {
  fail('实际执行测试数为 0（不允许只统计文件或接受空跑）');
}
if (counts.fail > 0 || testRun.status !== 0) {
  fail(`存在失败测试 ${counts.fail} 个，详见 ${path.relative(ROOT, TAP_LOG)}`);
}
if ((counts.skipped ?? 0) > 0 || (counts.todo ?? 0) > 0) {
  fail(`存在 skipped=${counts.skipped} / todo=${counts.todo} 的测试，视为未真实执行`);
}
if (counts.pass !== counts.tests) {
  fail(`通过数 ${counts.pass} 与执行数 ${counts.tests} 不一致`);
}
log(`   实际执行并通过 ${counts.pass}/${counts.tests} 个测试，无跳过无 TODO`);

// ⑤ CLI 冒烟：真实跑当前源码，核对三类结果、边界用例与可重跑稳定性。
const AT = '2026-11-14T09:30:00Z';
const cliArgs = [
  path.join(ROOT, 'app.mjs'),
  '--alerts',
  path.join(ROOT, 'fixtures', 'alerts.json'),
  '--exemptions',
  path.join(ROOT, 'fixtures', 'exemptions.json'),
  '--at',
  AT,
  '--out',
  CLI_REPORT,
];
const cliRun = spawnSync(NODE, cliArgs, { cwd: ROOT, encoding: 'utf8', timeout: 15_000 });
if (cliRun.status !== 0) {
  fail(`CLI 冒烟失败（退出码 ${cliRun.status}）：${cliRun.stderr}`);
}
let report;
try {
  report = JSON.parse(readFileSync(CLI_REPORT, 'utf8'));
} catch (err) {
  fail(`CLI 报告不是合法 JSON：${err.message}`);
}
const first = readFileSync(CLI_REPORT, 'utf8');
log(`⑤ CLI 冒烟报告已生成：${path.relative(ROOT, CLI_REPORT)}`);
log(`   summary=${JSON.stringify(report.summary)}`);

const checks = [
  ['三类桶齐备', ['carried', 'reopen', 'manual'].every((k) => Array.isArray(report.results?.[k]))],
  ['汇总计数一致', report.summary.carried + report.summary.reopen + report.summary.manual === report.summary.total],
  ['有效豁免可沿用', report.results.carried.some((r) => r.alert_id === 'ALRT-001' && r.reason_code === 'exemption_valid')],
  ['同类型不同位置重新打开', report.results.reopen.some((r) => r.alert_id === 'ALRT-002' && r.reason_code === 'location_mismatch')],
  ['规则版本变化转人工', report.results.manual.some((r) => r.alert_id === 'ALRT-003' && r.reason_code === 'rule_version_changed_unreconfirmed')],
  ['证据撤回转人工', report.results.manual.some((r) => r.alert_id === 'ALRT-004' && r.reason_code === 'evidence_withdrawn')],
  ['到期时刻边界仍沿用', report.results.carried.some((r) => r.alert_id === 'ALRT-008' && r.time_check?.at_valid_until_boundary === true)],
];
for (const [label, ok] of checks) {
  if (!ok) fail(`CLI 冒烟断言未通过：${label}`);
  log(`   ✔ ${label}`);
}

const rerun = spawnSync(NODE, cliArgs, { cwd: ROOT, encoding: 'utf8', timeout: 15_000 });
if (rerun.status !== 0) fail(`CLI 第二次运行失败：${rerun.stderr}`);
const second = readFileSync(CLI_REPORT, 'utf8');
if (first !== second) fail('同一输入两次运行报告不一致，稳定性断言失败');
log('   ✔ 同一输入重复运行，报告字节级稳定');

log('⑥ 全部核验通过。');
writeFileSync(SUMMARY_LOG, `${lines.join('\n')}\n`, 'utf8');
log(`   摘要：${path.relative(ROOT, SUMMARY_LOG)}`);
process.exit(0);
