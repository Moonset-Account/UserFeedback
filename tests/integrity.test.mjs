import { test, before, after, describe } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import { analyzeIntegrity, validateManifest, ManifestError } from '../src/integrity.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..');

const sha256 = (content) => createHash('sha256').update(content).digest('hex');

let tmpRoot;

before(async () => {
  tmpRoot = await mkdtemp(path.join(os.tmpdir(), 'integrity-gate-'));
});

after(async () => {
  await rm(tmpRoot, { recursive: true, force: true });
});

/** 每个夹具使用独立的新建目录，避免互串。 */
async function makeFixture(name, files) {
  const dir = path.join(tmpRoot, name);
  await mkdir(dir, { recursive: true });
  for (const [rel, content] of Object.entries(files)) {
    const full = path.join(dir, rel);
    await mkdir(path.dirname(full), { recursive: true });
    await writeFile(full, content);
  }
  return dir;
}

function buildManifest(overrides = {}) {
  return {
    entries: overrides.entries ?? [],
    approvals: overrides.approvals ?? [],
  };
}

function runCli(manifestPath, buildRoot) {
  return new Promise((resolve) => {
    const child = spawn(
      process.execPath,
      [path.join(repoRoot, 'src', 'cli.js'), '--manifest', manifestPath, '--build-root', buildRoot],
      { cwd: repoRoot },
    );
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => {
      stdout += chunk;
    });
    child.stderr.on('data', (chunk) => {
      stderr += chunk;
    });
    child.on('close', (code) => resolve({ code, stdout, stderr }));
  });
}

describe('发布完整性判定', () => {
  test('可接受结果：已发布入口的地区脚本与回退脚本均存在、字节摘要与批准一致时通过', async () => {
    const regionalJs = Buffer.from('/* region eu */ console.log("eu");\n');
    const fallbackJs = Buffer.from('/* global fallback */ console.log("global");\n');
    const buildRoot = await makeFixture('accept', {
      'js/home.eu.js': regionalJs,
      'js/home.fallback.js': fallbackJs,
    });

    const manifest = buildManifest({
      entries: [
        {
          id: 'home',
          route: '/home',
          status: 'published',
          branches: [
            {
              id: 'eu',
              region: 'eu',
              enabled: true,
              script: 'js/home.eu.js',
              fallback: {
                script: 'js/home.fallback.js',
                condition: 'region-unsupported',
              },
            },
          ],
        },
      ],
      approvals: [
        { script: 'js/home.eu.js', sha256: sha256(regionalJs), approvedBy: 'rel-bot', summary: 'release-1' },
        { script: 'js/home.fallback.js', sha256: sha256(fallbackJs), approvedBy: 'rel-bot', summary: 'release-1' },
      ],
    });

    const result = await analyzeIntegrity({ manifest, buildRoot });

    assert.equal(result.ok, true);
    assert.deepEqual(result.violations, []);
    assert.equal(result.entries[0].enabledBranchCount, 1);
    // 主脚本 + 回退脚本都必须被实际核算，而不是只核地区脚本
    const chain = result.entries[0].branches[0].chain;
    assert.deepEqual(
      chain.map((n) => [n.kind, n.script, n.approved]),
      [
        ['primary', 'js/home.eu.js', true],
        ['fallback', 'js/home.fallback.js', true],
      ],
    );
    assert.equal(chain[0].sha256, sha256(regionalJs));
    assert.equal(chain[1].approval.summary, 'release-1');
  });

  test('应被阻止：同名脚本更换字节但批准摘要仍旧时报告 DIGEST_MISMATCH 并阻断', async () => {
    const approvedBytes = Buffer.from('console.log("approved version");\n');
    const tamperedBytes = Buffer.from('console.log("swapped same-name content");\n');
    assert.notEqual(sha256(approvedBytes), sha256(tamperedBytes));
    const buildRoot = await makeFixture('tamper', {
      'js/home.eu.js': tamperedBytes,
    });

    const manifest = buildManifest({
      entries: [
        {
          id: 'home',
          status: 'published',
          branches: [
            { id: 'eu', region: 'eu', script: 'js/home.eu.js' },
          ],
        },
      ],
      approvals: [
        // 批准的是旧字节：同名脚本换内容必须被挡住
        { script: 'js/home.eu.js', sha256: sha256(approvedBytes), approvedBy: 'rel-bot' },
      ],
    });

    const result = await analyzeIntegrity({ manifest, buildRoot });

    assert.equal(result.ok, false);
    assert.equal(result.violations.length, 1);
    const violation = result.violations[0];
    assert.equal(violation.code, 'DIGEST_MISMATCH');
    assert.equal(violation.entryId, 'home');
    assert.equal(violation.branchId, 'eu');
    assert.equal(violation.script, 'js/home.eu.js');
    assert.equal(violation.actualSha256, sha256(tamperedBytes));
    assert.deepEqual(violation.approvedSha256, [sha256(approvedBytes)]);
  });

  test('应被阻止：只批准地区脚本、漏批运行时仍可达的回退脚本时报告 APPROVAL_MISSING', async () => {
    const regionalJs = Buffer.from('regional\n');
    const fallbackJs = Buffer.from('fallback\n');
    const buildRoot = await makeFixture('missing-approval', {
      'js/home.eu.js': regionalJs,
      'js/home.fallback.js': fallbackJs,
    });

    const manifest = buildManifest({
      entries: [
        {
          id: 'home',
          status: 'published',
          branches: [
            {
              id: 'eu',
              region: 'eu',
              script: 'js/home.eu.js',
              fallback: { script: 'js/home.fallback.js', condition: 'cdn-error' },
            },
          ],
        },
      ],
      approvals: [
        // 只核了地区脚本，回退脚本没有任何批准
        { script: 'js/home.eu.js', sha256: sha256(regionalJs) },
      ],
    });

    const result = await analyzeIntegrity({ manifest, buildRoot });

    assert.equal(result.ok, false);
    const codes = result.violations.map((v) => v.code);
    assert.deepEqual(codes, ['APPROVAL_MISSING']);
    const violation = result.violations[0];
    assert.equal(violation.script, 'js/home.fallback.js');
    assert.equal(violation.kind, 'fallback');
    assert.equal(violation.actualSha256, sha256(fallbackJs));
  });

  test('应被阻止：可达资源在构建产物中缺件时报告 SCRIPT_MISSING，并指出入口与分支', async () => {
    const buildRoot = await makeFixture('missing-file', {
      'js/home.eu.js': Buffer.from('regional\n'),
      // 回退脚本 js/home.fallback.js 未随构建导出
    });

    const manifest = buildManifest({
      entries: [
        {
          id: 'home',
          status: 'published',
          branches: [
            {
              id: 'eu',
              region: 'eu',
              script: 'js/home.eu.js',
              fallback: { script: 'js/home.fallback.js', condition: 'region-unsupported' },
            },
          ],
        },
      ],
      approvals: [
        { script: 'js/home.eu.js', sha256: sha256(Buffer.from('regional\n')) },
        // 即使清单里有批准，构建缺件仍然必须阻断
        { script: 'js/home.fallback.js', sha256: sha256(Buffer.from('fallback\n')) },
      ],
    });

    const result = await analyzeIntegrity({ manifest, buildRoot });

    assert.equal(result.ok, false);
    const violation = result.violations.find((v) => v.code === 'SCRIPT_MISSING');
    assert.ok(violation, '应报告 SCRIPT_MISSING');
    assert.equal(violation.entryId, 'home');
    assert.equal(violation.branchId, 'eu');
    assert.equal(violation.script, 'js/home.fallback.js');
  });

  test('交错场景：未发布预览入口的违规不阻塞健康的正式入口，但必须留痕', async () => {
    const goodRegional = Buffer.from('good regional\n');
    const goodFallback = Buffer.from('good fallback\n');
    const previewBytes = Buffer.from('preview draft\n');
    const buildRoot = await makeFixture('preview-interleave', {
      'js/home.eu.js': goodRegional,
      'js/home.fallback.js': goodFallback,
      'js/preview.js': previewBytes, // 存在但未批准
    });

    const manifest = buildManifest({
      entries: [
        {
          id: 'home',
          status: 'published',
          branches: [
            {
              id: 'eu',
              region: 'eu',
              script: 'js/home.eu.js',
              fallback: { script: 'js/home.fallback.js' },
            },
          ],
        },
        {
          id: 'home-next-preview',
          status: 'preview',
          branches: [{ id: 'draft', script: 'js/preview.js' }],
        },
      ],
      approvals: [
        { script: 'js/home.eu.js', sha256: sha256(goodRegional) },
        { script: 'js/home.fallback.js', sha256: sha256(goodFallback) },
      ],
    });

    const result = await analyzeIntegrity({ manifest, buildRoot });

    assert.equal(result.ok, true, '正式入口健康时门禁应通过');
    assert.deepEqual(result.violations, []);
    assert.equal(result.previewViolations.length, 1);
    assert.equal(result.previewViolations[0].code, 'APPROVAL_MISSING');
    assert.equal(result.previewViolations[0].entryId, 'home-next-preview');
    const previewReport = result.entries.find((e) => e.id === 'home-next-preview');
    assert.equal(previewReport.blocking, false);
    assert.equal(previewReport.branches[0].chain[0].sha256, sha256(previewBytes));
  });

  test('边界：显式禁用分支不参与核算；正式入口所有分支禁用时不产生违规并标记跳过', async () => {
    const buildRoot = await makeFixture('disabled-branch', {
      'js/home.eu.js': Buffer.from('eu\n'),
    });
    const manifest = buildManifest({
      entries: [
        {
          id: 'home',
          status: 'published',
          branches: [
            // 禁用分支引用了不存在也未批准的脚本，不应触发任何违规
            { id: 'eu', region: 'eu', enabled: false, script: 'js/home.eu.js' },
          ],
        },
      ],
      approvals: [],
    });

    const result = await analyzeIntegrity({ manifest, buildRoot });

    assert.equal(result.ok, true);
    assert.deepEqual(result.violations, []);
    assert.deepEqual(result.skippedEntries, [{ id: 'home', reason: 'no-enabled-branch' }]);
    assert.equal(result.entries[0].enabledBranchCount, 0);
  });

  test('边界：多级回退链逐级核算，深层回退缺件同样阻断', async () => {
    const primary = Buffer.from('primary\n');
    const fb1 = Buffer.from('fallback-1\n');
    // 第二级回退脚本未导出
    const buildRoot = await makeFixture('deep-fallback', {
      'js/p.js': primary,
      'js/f1.js': fb1,
    });
    const manifest = buildManifest({
      entries: [
        {
          id: 'home',
          status: 'published',
          branches: [
            {
              id: 'default',
              script: 'js/p.js',
              fallback: {
                script: 'js/f1.js',
                condition: 'primary-failed',
                fallback: { script: 'js/f2.js', condition: 'f1-failed' },
              },
            },
          ],
        },
      ],
      approvals: [
        { script: 'js/p.js', sha256: sha256(primary) },
        { script: 'js/f1.js', sha256: sha256(fb1) },
        { script: 'js/f2.js', sha256: sha256(Buffer.from('f2\n')) },
      ],
    });

    const result = await analyzeIntegrity({ manifest, buildRoot });

    assert.equal(result.ok, false);
    const chain = result.entries[0].branches[0].chain;
    assert.deepEqual(chain.map((n) => n.kind), ['primary', 'fallback', 'fallback']);
    const violation = result.violations[0];
    assert.equal(violation.code, 'SCRIPT_MISSING');
    assert.equal(violation.script, 'js/f2.js');
  });

  test('边界：引用路径越出构建目录时报告 PATH_TRAVERSAL，不允许读取外部文件', async () => {
    const buildRoot = await makeFixture('traversal', {
      'js/home.eu.js': Buffer.from('eu\n'),
    });
    const manifest = buildManifest({
      entries: [
        {
          id: 'home',
          status: 'published',
          branches: [
            { id: 'eu', script: 'js/home.eu.js', fallback: { script: '../../etc/secret.js' } },
          ],
        },
      ],
      approvals: [{ script: 'js/home.eu.js', sha256: sha256(Buffer.from('eu\n')) }],
    });

    const result = await analyzeIntegrity({ manifest, buildRoot });

    assert.equal(result.ok, false);
    const violation = result.violations.find((v) => v.code === 'PATH_TRAVERSAL');
    assert.ok(violation);
    assert.equal(violation.script, '../../etc/secret.js');
  });

  test('清单结构不合法时抛 ManifestError，而不是静默放行', () => {
    assert.throws(() => validateManifest({}), (err) => err instanceof ManifestError);
    assert.throws(
      () =>
        validateManifest({
          entries: [{ id: 'x', branches: [{ id: 'b', script: 'js/x.js' }] }],
          approvals: [{ script: 'js/x.js', sha256: 'not-a-hash' }],
        }),
      (err) => err instanceof ManifestError && /sha256/.test(err.message),
    );
    assert.throws(
      () =>
        validateManifest({
          entries: [
            {
              id: 'x',
              branches: [
                { id: 'b1', script: 'a.js' },
                { id: 'b1', script: 'c.js' },
              ],
            },
          ],
        }),
      (err) => err instanceof ManifestError && /分支 id 重复/.test(err.message),
    );
  });

  test('CLI：通过场景退出 0，阻断场景退出 1 且输出指明入口与分支', async () => {
    const good = Buffer.from('good\n');
    const bad = Buffer.from('tampered\n');
    const approvedGood = Buffer.from('approved-good\n');
    const buildOk = await makeFixture('cli-ok', { 'js/a.js': good });
    const buildBad = await makeFixture('cli-bad', { 'js/a.js': bad });

    const okManifest = path.join(buildOk, 'manifest.json');
    await writeFile(
      okManifest,
      JSON.stringify(
        buildManifest({
          entries: [{ id: 'home', status: 'published', branches: [{ id: 'eu', script: 'js/a.js' }] }],
          approvals: [{ script: 'js/a.js', sha256: sha256(good) }],
        }),
      ),
    );

    const badManifest = path.join(buildBad, 'manifest.json');
    await writeFile(
      badManifest,
      JSON.stringify(
        buildManifest({
          entries: [{ id: 'home', status: 'published', branches: [{ id: 'eu', script: 'js/a.js' }] }],
          approvals: [{ script: 'js/a.js', sha256: sha256(approvedGood) }],
        }),
      ),
    );

    const okRun = await runCli(okManifest, buildOk);
    assert.equal(okRun.code, 0);
    assert.match(okRun.stdout, /判定结论: 通过/);

    const badRun = await runCli(badManifest, buildBad);
    assert.equal(badRun.code, 1);
    assert.match(badRun.stdout, /DIGEST_MISMATCH/);
    assert.match(badRun.stdout, /入口=home/);
    assert.match(badRun.stdout, /分支=eu/);
  });
});
