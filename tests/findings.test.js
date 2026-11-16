import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fingerprint, finalize, inlineSuppression, configSuppression, meetsThreshold, applyExcludes } from '../src/findings.js';
import { globToRegExp, matchesAny, relPosix } from '../src/util.js';

const sast = (over = {}) => finalize({
  tool: 'sighthound', category: 'sast', ruleId: 'sighthound/ssrf', severity: 'high', message: 'm',
  file: 'src/a.js', line: 10, snippet: 'const r = await fetch(req.query.url)', ...over,
});

test('fingerprint ignores line number and whitespace for SAST', () => {
  const a = sast();
  const b = sast({ line: 42, snippet: 'const r = await   fetch(req.query.url)  ' });
  assert.equal(a.fingerprint, b.fingerprint);
});

test('fingerprint changes with file, rule, or code', () => {
  const a = sast();
  assert.notEqual(a.fingerprint, sast({ file: 'src/b.js' }).fingerprint);
  assert.notEqual(a.fingerprint, sast({ ruleId: 'sighthound/xss' }).fingerprint);
  assert.notEqual(a.fingerprint, sast({ snippet: 'fetch(other)' }).fingerprint);
});

test('supply-chain fingerprint keys on advisory + package + version + lockfile', () => {
  const f = finalize({ tool: 'osv-scanner', category: 'supply-chain', ruleId: 'osv/GHSA-1', severity: 'high', message: 'm', file: 'package-lock.json', extra: { package: 'lodash', version: '4.17.20' } });
  const g = finalize({ tool: 'osv-scanner', category: 'supply-chain', ruleId: 'osv/GHSA-1', severity: 'high', message: 'different text', file: 'package-lock.json', extra: { package: 'lodash', version: '4.17.20' } });
  const h = finalize({ tool: 'osv-scanner', category: 'supply-chain', ruleId: 'osv/GHSA-1', severity: 'high', message: 'm', file: 'package-lock.json', extra: { package: 'lodash', version: '4.17.21' } });
  assert.equal(f.fingerprint, g.fingerprint);
  assert.notEqual(f.fingerprint, h.fingerprint);
});

test('secrets fingerprint uses the secret hash, not the line', () => {
  const a = finalize({ tool: 'gitleaks', category: 'secrets', ruleId: 'gitleaks/github-pat', severity: 'high', message: 'm', file: 'c.js', line: 2, extra: { secretHash: 'abc' } });
  const b = finalize({ tool: 'gitleaks', category: 'secrets', ruleId: 'gitleaks/github-pat', severity: 'high', message: 'm', file: 'c.js', line: 9, extra: { secretHash: 'abc' } });
  assert.equal(a.fingerprint, b.fingerprint);
});

test('inline suppression on the same line and the line above, with optional rule id', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'secscan-'));
  fs.writeFileSync(path.join(dir, 'a.js'), [
    'const x = 1;',
    'fetch(url); // secscan-ignore: ssrf allow-listed upstream',
    '// secscan-ignore: sighthound/xss reviewed',
    'el.innerHTML = y;',
    'exec(cmd); // secscan-ignore: xss (wrong rule)',
    'plain(); // secscan-ignore',
  ].join('\n'));
  const cache = new Map();
  const ssrf = inlineSuppression(dir, { file: 'a.js', line: 2, ruleId: 'sighthound/ssrf', tool: 'sighthound' }, cache);
  assert.equal(ssrf?.kind, 'inline');
  assert.match(ssrf.reason, /allow-listed/);
  const xss = inlineSuppression(dir, { file: 'a.js', line: 4, ruleId: 'sighthound/xss', tool: 'sighthound' }, cache);
  assert.equal(xss?.kind, 'inline');
  const wrong = inlineSuppression(dir, { file: 'a.js', line: 5, ruleId: 'sighthound/command-injection', tool: 'sighthound' }, cache);
  assert.equal(wrong, null);
  const bare = inlineSuppression(dir, { file: 'a.js', line: 6, ruleId: 'anything/at-all', tool: 'x' }, cache);
  assert.equal(bare?.kind, 'inline');
});

test('config suppression matches rule suffix and path glob, honours expiry', () => {
  const f = { tool: 'sighthound', ruleId: 'sighthound/path-traversal', file: 'scripts/batch.js' };
  assert.ok(configSuppression(f, [{ rule: 'path-traversal', path: 'scripts/**', reason: 'cli only' }]));
  assert.equal(configSuppression(f, [{ rule: 'path-traversal', path: 'src/**', reason: 'no' }]), null);
  assert.equal(configSuppression(f, [{ rule: 'path-traversal', reason: 'expired', expires: '2000-01-01' }]), null);
  assert.ok(configSuppression(f, [{ tool: 'sighthound', reason: 'whole tool' }]));
  assert.equal(configSuppression(f, [{ reason: 'empty rule must not match everything' }]), null);
});

test('threshold comparison', () => {
  assert.ok(meetsThreshold('critical', 'high'));
  assert.ok(meetsThreshold('high', 'high'));
  assert.ok(!meetsThreshold('medium', 'high'));
  assert.ok(!meetsThreshold('critical', 'none'));
});

test('glob matching', () => {
  assert.ok(matchesAny('a/node_modules/x/y.js', ['**/node_modules/**']));
  assert.ok(matchesAny('lib/app.min.js', ['**/*.min.js']));
  assert.ok(!matchesAny('lib/app.js', ['**/*.min.js']));
  assert.ok(matchesAny('scripts/batch.js', ['scripts/**']));
  assert.ok(globToRegExp('/src/*.js').test('src/a.js'));
  assert.ok(!globToRegExp('/src/*.js').test('x/src/a.js'));
});

test('excludes drop findings of every category under the excluded path', () => {
  const list = [sast({ file: 'dist/bundle.js' }), sast({ file: 'src/ok.js' }),
    finalize({ tool: 'osv-scanner', category: 'supply-chain', ruleId: 'osv/x', severity: 'low', message: 'm', file: 'dist/package-lock.json', extra: { package: 'a', version: '1' } }),
    finalize({ tool: 'osv-scanner', category: 'supply-chain', ruleId: 'osv/y', severity: 'low', message: 'm', file: 'package-lock.json', extra: { package: 'b', version: '1' } })];
  const out = applyExcludes(list, ['**/dist/**']);
  assert.deepEqual(out.map((f) => f.file), ['src/ok.js', 'package-lock.json']);
});

test('relPosix normalises separators and stays inside root', () => {
  const root = path.resolve('/tmp/repo');
  assert.equal(relPosix(root, path.join(root, 'a', 'b.js')), 'a/b.js');
  assert.equal(relPosix(root, './a/b.js'), 'a/b.js');
});
