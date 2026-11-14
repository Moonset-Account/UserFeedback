#!/usr/bin/env node
// 本地核验包装器：真实执行当前源码与测试，并校验证据。
// 仅使用 Node.js 标准模块；不安装任何依赖。
// 用法: node tools/local_verify.mjs
// 退出码: 0 = 全部通过；1 = 测试失败/零测试/全部跳过/证据不足/环境异常。

import { spawnSync } from 'node:child_process';
import { readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const node = process.execPath;
const failures = [];
const testFiles = readdirSync(path.join(root, 'tests'))
  .filter((f) => f.endsWith('.test.mjs'))
  .sort()
  .map((f) => path.join(root, 'tests', f));

function check(name, ok, evidence) {
  const mark = ok ? 'PASS' : 'FAIL';
  process.stdout.write(`[verify:${mark}] ${name} — ${evidence}\n`);
  if (!ok) failures.push(name);
}

// 1) 真实执行测试套件（TAP 输出，保留原始输出）。
process.stdout.write(`=== node --test 原始输出开始（测试文件 ${testFiles.length} 个）===\n`);
const t = spawnSync(node, ['--test', '--test-reporter=tap', ...testFiles], {
  encoding: 'utf8',
  cwd: root,
});
process.stdout.write(t.stdout ?? '');
process.stderr.write(t.stderr ?? '');
process.stdout.write('=== node --test 原始输出结束 ===\n');

const tap = t.stdout ?? '';
const summaryNum = (name) => {
  const m = tap.match(new RegExp(`^# ${name} (\\d+)\\s*$`, 'm'));
  return m ? Number(m[1]) : null;
};
const pass = summaryNum('pass');
const fail = summaryNum('fail');
const skipped = summaryNum('skipped') ?? 0;
const cancelled = summaryNum('cancelled') ?? 0;
const executed = (pass ?? 0) + (fail ?? 0);

check('test-files-present', testFiles.length >= 1, `测试文件: ${testFiles.map((f) => path.basename(f)).join(', ') || '(无)'}`);
check('test-process-exit', t.status === 0 && !t.error, `exit=${t.status} error=${t.error ?? 'none'}`);
check('test-summary-present', pass !== null && fail !== null, `pass=${pass} fail=${fail} skipped=${skipped} cancelled=${cancelled}`);
check('test-executed>=1', executed >= 1, `实际执行测试数 executed=${executed}`);
check('test-not-all-skipped', executed > 0 && skipped < executed + skipped, `skipped=${skipped} executed=${executed}`);
check('test-zero-failures', fail === 0 && cancelled === 0, `fail=${fail} cancelled=${cancelled}`);

// 2) 真实执行当前源码（CLI 双快照模式），校验 JSON 报告语义。
const cli = spawnSync(node, [
  path.join(root, 'app.mjs'),
  '--snapshot-a', path.join(root, 'fixtures', 'snapshot-a'),
  '--snapshot-b', path.join(root, 'fixtures', 'snapshot-b'),
], { encoding: 'utf8', cwd: root });
let report = null;
try {
  report = JSON.parse(cli.stdout);
} catch {
  report = null;
}
check('cli-exit', cli.status === 0, `exit=${cli.status} stderr=${(cli.stderr ?? '').trim() || '(empty)'}`);
check('cli-json-report', report !== null && report.tool === 'trusted-types-coverage-audit',
  report ? `tool=${report.tool} findings=${report.summary?.findings}` : `stdout 不是合法 JSON: ${(cli.stdout ?? '').slice(0, 200)}`);
check('cli-findings-present', Array.isArray(report?.findings) && report.findings.length > 0,
  `findings=${report?.findings?.length ?? 'n/a'}`);
check('cli-finding-fields', Array.isArray(report?.findings) && report.findings.every(
  (f) => f.id && f.category && Array.isArray(f.entries) && Array.isArray(f.loadPaths) && typeof f.fixPoint === 'string',
), '每项 finding 均含 id/category/entries/loadPaths/fixPoint');

if (failures.length > 0) {
  process.stdout.write(`VERIFY FAILED: ${failures.join(', ')}\n`);
  process.exit(1);
}
process.stdout.write(`VERIFY OK: 执行测试 ${executed} 项，CLI 报告 findings ${report.findings.length} 项。\n`);
process.exit(0);
