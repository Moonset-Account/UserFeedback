#!/usr/bin/env node
// 本地核验入口：真实执行单元测试与 CLI 冒烟检查，保留原始输出。
// 退出码：0 = 全部通过且实际执行测试数 >= 1；非 0 = 失败 / 零测试 / 异常。
import { spawnSync } from 'node:child_process';
import { readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const failures = [];

function section(title) {
  process.stdout.write(`\n===== ${title} =====\n`);
}

// 1) 运行 node:test 测试套件（真实执行，保留原始输出）
const testFiles = readdirSync(path.join(root, 'tests'))
  .filter((f) => f.endsWith('.test.mjs'))
  .sort()
  .map((f) => path.join('tests', f));
if (testFiles.length === 0) {
  failures.push('no test files found under tests/');
}
section(`node --test ${testFiles.join(' ')}`);
const testRun = spawnSync(process.execPath, ['--test', ...testFiles], {
  cwd: root,
  encoding: 'utf8',
  timeout: 60000,
});
if (testRun.error) {
  failures.push(`test runner failed to start: ${testRun.error.message}`);
}
const testOut = `${testRun.stdout ?? ''}${testRun.stderr ?? ''}`;
process.stdout.write(testOut);
if (testRun.status !== 0) {
  failures.push(`node --test exited with status ${testRun.status}`);
}
const passMatch = testOut.match(/^(?:#|ℹ) pass (\d+)$/m);
const failMatch = testOut.match(/^(?:#|ℹ) fail (\d+)$/m);
const passCount = passMatch ? Number(passMatch[1]) : 0;
const failCount = failMatch ? Number(failMatch[1]) : 0;
process.stdout.write(`\n[verify] parsed test summary: pass=${passCount} fail=${failCount}\n`);
if (passCount < 1) {
  failures.push('zero tests actually executed (pass count < 1)');
}
if (failCount > 0) {
  failures.push(`${failCount} test(s) failed`);
}

// 2) CLI 冒烟：样例批次必须覆盖四类验收场景
section('CLI smoke: fixtures/sample-batch.json');
const cliRun = spawnSync(
  process.execPath,
  [path.join(root, 'app.mjs'), path.join(root, 'fixtures', 'sample-batch.json'), '--json'],
  { cwd: root, encoding: 'utf8', timeout: 30000 },
);
process.stdout.write(cliRun.stdout ?? '');
if (cliRun.stderr) process.stdout.write(cliRun.stderr);
if (cliRun.status !== 2) {
  failures.push(`sample batch should exit 2 (boundary divergence present), got ${cliRun.status}`);
} else {
  let report = null;
  try {
    report = JSON.parse(cliRun.stdout);
  } catch (err) {
    failures.push(`sample batch output is not valid JSON: ${err.message}`);
  }
  if (report) {
    const checks = [
      ['连续多条正常记录', report.summary.normal_forward >= 3],
      [
        '两端都正确结束',
        report.findings.some(
          (f) =>
            f.case === 'normal_forward' &&
            f.connection_closed?.gateway === true &&
            f.connection_closed?.app === true,
        ),
      ],
      [
        '网关已拒绝但关闭状态缺失',
        report.findings.some(
          (f) => f.case === 'gateway_reject' && f.close_status === 'missing_close_evidence',
        ),
      ],
      [
        '同一条消息边界不一致',
        report.findings.some(
          (f) => f.case === 'boundary_divergence' && f.divergence === 'range_mismatch',
        ),
      ],
      [
        '拒绝场景给出待检查配置项',
        report.findings.every(
          (f) => f.case !== 'gateway_reject' || (f.pending_config_checks ?? []).length > 0,
        ),
      ],
    ];
    for (const [name, ok] of checks) {
      process.stdout.write(`[verify] acceptance "${name}": ${ok ? 'OK' : 'MISSING'}\n`);
      if (!ok) failures.push(`acceptance scenario not covered by sample batch: ${name}`);
    }
  }
}

// 3) 重复导入稳定性：同一文件两次运行输出必须一致
section('re-import stability');
const rerun = spawnSync(
  process.execPath,
  [path.join(root, 'app.mjs'), path.join(root, 'fixtures', 'sample-batch.json'), '--json'],
  { cwd: root, encoding: 'utf8', timeout: 30000 },
);
if (rerun.stdout === cliRun.stdout && rerun.status === cliRun.status) {
  process.stdout.write('[verify] re-import stability: OK (identical output)\n');
} else {
  failures.push('re-import produced different output');
}

section('verify result');
if (failures.length > 0) {
  for (const f of failures) process.stdout.write(`FAIL: ${f}\n`);
  process.exit(1);
}
process.stdout.write(`OK: ${passCount} test(s) passed, CLI smoke passed.\n`);
process.exit(0);
