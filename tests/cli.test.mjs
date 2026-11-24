import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const cli = path.join(root, 'src', 'cli.mjs');
const fixture = path.join(root, 'examples', 'synthetic-package.mjs');
const minimalJson = path.join(root, 'examples', 'minimal-package.json');

test('CLI prints text report and exits non-zero for blocking errors', () => {
  const result = spawnSync(process.execPath, [cli, fixture], { encoding: 'utf8' });
  assert.equal(result.status, 1);
  assert.match(result.stdout, /Extra install-time permissions/);
  assert.match(result.stdout, /management \[feature=diagnostics\]/);
  assert.match(result.stdout, /RESIDUAL_OPTIONAL_REQUEST/);
});

test('CLI --json emits a parseable structured report', () => {
  const result = spawnSync(process.execPath, [cli, fixture, '--json'], { encoding: 'utf8', maxBuffer: 2_000_000 });
  assert.equal(result.status, 1);
  const report = JSON.parse(result.stdout);
  assert.equal(report.packageName, 'synthetic-clipper');
  assert.equal(report.summary.extraInstallPermissions, 1);
  assert.equal(report.variants.length, 2);
});

test('CLI accepts a JSON manifest and exits zero for an approved minimal package', () => {
  const result = spawnSync(process.execPath, [cli, minimalJson], { encoding: 'utf8' });
  assert.equal(result.status, 0);
  assert.match(result.stdout, /RELEASE REVIEW: APPROVED/);
});

test('CLI help exits successfully', () => {
  const result = spawnSync(process.execPath, [cli, '--help'], { encoding: 'utf8' });
  assert.equal(result.status, 0);
  assert.match(result.stdout, /Usage: extension-review/);
});
