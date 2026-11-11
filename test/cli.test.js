import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const cli = join(here, '..', 'src', 'cli.js');
const example = join(here, '..', 'examples', 'manifest.example.json');

function run(args) {
  const res = spawnSync(process.execPath, [cli, ...args], { encoding: 'utf8' });
  return { status: res.status, stdout: res.stdout, stderr: res.stderr };
}

test('文本模式：渲染每个构建的理由、预算、总耗时与超预算诊断，退出码 2', () => {
  const r = run([example]);
  assert.equal(r.status, 2, '示例含 windows 超预算构建，退出码应为 2');
  assert.match(r.stdout, /构建 linux-x64/);
  assert.match(r.stdout, /可行（预算内）/);
  assert.match(r.stdout, /穷举（已证明最优）/);
  assert.match(r.stdout, /SEC-001/);
  assert.match(r.stdout, /强制保留/);
  assert.match(r.stdout, /传递依赖带入/);
  assert.match(r.stdout, /构建 windows-x64 —— 超预算/);
  assert.match(r.stdout, /OPTIMAL_OVER_BUDGET/);
  assert.match(r.stdout, /超支 3 分钟/);
  assert.match(r.stdout, /依赖环警告/);
  assert.match(r.stdout, /SEC-010 -> SEC-011 -> SEC-010/);
});

test('JSON 模式输出机器可读结构', () => {
  const r = run([example, '--json']);
  assert.equal(r.status, 2);
  const parsed = JSON.parse(r.stdout);
  assert.ok(Array.isArray(parsed.builds));
  assert.equal(parsed.summary.feasible, 2);
  assert.equal(parsed.summary.overBudget, 1);
  const linux = parsed.builds.find((b) => b.build === 'linux-x64');
  assert.equal(linux.totalMinutes, 19);
  assert.ok(linux.reasons.every((x) => typeof x.detail === 'string' && x.detail.length > 0));
  assert.ok(parsed.cycleWarnings.some((w) => w.build === 'linux-x64'));
});

test('--build 只求解指定构建；--exhaustive-limit 0 切换贪心', () => {
  const r = run([example, '--json', '--build', 'arm64', '--exhaustive-limit', '0']);
  assert.equal(r.status, 0);
  const parsed = JSON.parse(r.stdout);
  assert.equal(parsed.builds.length, 1);
  assert.equal(parsed.builds[0].build, 'arm64');
  assert.equal(parsed.builds[0].strategy, 'greedy');
});

test('清单校验失败退出码 3 且逐条列出问题', () => {
  const dir = mkdtempSync(join(tmpdir(), 'sec-manifest-'));
  const bad = join(dir, 'bad.json');
  writeFileSync(
    bad,
    JSON.stringify({
      builds: [{ id: 'linux', budgetMinutes: 1, requiredFixes: [] }],
      cases: [
        { id: 'X', durationMinutes: 1, builds: ['linux'], fixes: [] },
        { id: 'X', durationMinutes: 1, builds: ['linux'], fixes: [] },
      ],
    }),
  );
  const text = run([bad]);
  assert.equal(text.status, 3);
  assert.match(text.stderr, /VALIDATION_FAILED/);
  assert.match(text.stderr, /DUPLICATE_CASE_SCOPE_OVERLAP/);

  const json = run([bad, '--json']);
  assert.equal(json.status, 3);
  const parsed = JSON.parse(json.stdout);
  assert.equal(parsed.error.code, 'VALIDATION_FAILED');
});

test('缺失文件参数时报用法错误（退出码非 0）', () => {
  const r = run([]);
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /用法/);
});
