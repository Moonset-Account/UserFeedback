import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync, spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';

// gitleaks applies an entropy floor, so filler like 'a'.repeat(24) is not a token; this is.
const rand = (n) => randomBytes(n * 2).toString('base64').replace(/[^a-zA-Z0-9]/g, '').slice(0, n);
import { resolveTool } from '../src/tools.js';
import { buildBaseline, applyBaseline, resolvedSince } from '../src/baseline.js';
import { finalize } from '../src/findings.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BIN = path.join(HERE, '..', 'bin', 'secscan.js');
const FIXTURE = path.join(HERE, '..', 'fixtures', 'node-vuln');
const PY_FIXTURE = path.join(HERE, '..', 'fixtures', 'python-vuln');

function secscan(args, cwd) {
  const r = spawnSync(process.execPath, [BIN, ...args], { cwd, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  return { status: r.status, stdout: r.stdout, stderr: r.stderr };
}

test('baseline: accepted findings are hidden, new ones surface, fixed ones resolve', () => {
  const a = finalize({ tool: 't', category: 'sast', ruleId: 't/a', severity: 'high', message: 'm', file: 'a.js', line: 1, snippet: 'x' });
  const b = finalize({ tool: 't', category: 'sast', ruleId: 't/b', severity: 'high', message: 'm', file: 'b.js', line: 1, snippet: 'y' });
  const base = buildBaseline(FIXTURE, [a, b], { t: '1' }, null);
  assert.equal(Object.keys(base.findings).length, 2);
  const c = finalize({ tool: 't', category: 'sast', ruleId: 't/c', severity: 'low', message: 'm', file: 'c.js', line: 1, snippet: 'z' });
  const now = [finalize({ ...a }), c];
  applyBaseline(now, base);
  assert.equal(now[0].status, 'baseline');
  assert.equal(now[1].status, 'new');
  const gone = resolvedSince(now, base);
  assert.deepEqual(gone.map((g) => g.ruleId), ['t/b']);
});

test('cli: detect and help run without any tool installed', () => {
  const h = secscan(['--help'], FIXTURE);
  assert.equal(h.status, 0);
  assert.match(h.stdout, /secscan \d/);
  const d = secscan(['detect', FIXTURE], FIXTURE);
  assert.equal(d.status, 0);
  assert.match(d.stdout, /javascript/);
  assert.match(d.stdout, /npm x1/);
});

test('cli: fixture scan with whatever tools are installed', { skip: !resolveTool('sighthound').path && !resolveTool('gitleaks').path ? 'no scanners installed' : false }, () => {
  const r = secscan(['scan', FIXTURE, '--no-baseline', '--format', 'json', '--quiet'], FIXTURE);
  assert.equal(r.status, 0, r.stderr);
  const j = JSON.parse(r.stdout);
  assert.ok(j.findings.length > 0);
  if (resolveTool('sighthound').path) {
    const cmd = j.findings.find((f) => f.ruleId === 'sighthound/command-injection');
    assert.ok(cmd, 'sighthound finds the command injection');
    assert.equal(cmd.file, 'app.js');
  }
  if (resolveTool('gitleaks').path) {
    const s = j.findings.find((f) => f.category === 'secrets');
    assert.ok(s, 'gitleaks finds the synthetic token');
    assert.ok(!r.stdout.includes('ghp_' + 'dIQ1'), 'secret must not appear in output');
  }
  const sc = j.findings.filter((f) => f.category === 'supply-chain');
  if (resolveTool('osv-scanner').path) assert.ok(sc.some((f) => f.extra.package === 'minimist'), 'osv finds minimist');
  // fail-on makes the exit code non-zero
  const r2 = secscan(['scan', FIXTURE, '--no-baseline', '--fail-on', 'critical', '--quiet'], FIXTURE);
  assert.equal(r2.status, 1);
});

test('cli: init writes config + baseline in a temp git repo, then a rescan is clean', { skip: !resolveTool('gitleaks').path ? 'gitleaks needed' : false }, () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'secscan-e2e-'));
  for (const f of fs.readdirSync(FIXTURE)) fs.copyFileSync(path.join(FIXTURE, f), path.join(dir, f));
  execFileSync('git', ['init', '-q'], { cwd: dir });
  execFileSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@t', 'add', '-A'], { cwd: dir });
  execFileSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init'], { cwd: dir });
  const init = secscan(['init', '--hook', '--quiet'], dir);
  assert.equal(init.status, 0, init.stderr);
  assert.ok(fs.existsSync(path.join(dir, '.secscan.json')));
  assert.ok(fs.existsSync(path.join(dir, '.secscan', 'baseline.json')));
  assert.ok(fs.existsSync(path.join(dir, '.secscan', 'invariants.md')));
  const hook = fs.readFileSync(path.join(dir, '.git', 'hooks', 'pre-commit'), 'utf8');
  assert.match(hook, /secscan pre-commit hook/);
  const again = secscan(['scan', '--format', 'json', '--quiet'], dir);
  assert.equal(again.status, 0);
  const j = JSON.parse(again.stdout);
  assert.equal(j.counts.new, 0, 'everything is baselined');
  // a newly staged secret is blocked in hook mode
  // secscan-ignore: slack-bot-token synthetic value assembled at runtime for the hook test
  fs.writeFileSync(path.join(dir, 'leak.js'), "export const T = 'xoxb-123456789012-1234567890123-" + rand(24) + "';\n");
  execFileSync('git', ['add', 'leak.js'], { cwd: dir });
  const hookRun = secscan(['scan', '--hook', '--quiet'], dir);
  assert.equal(hookRun.status, 1, 'hook mode fails on a new secret');
  assert.match(hookRun.stdout, /gitleaks\//);
  // a secret in a gitignored local file is reported as low, labelled, and never blocks
  execFileSync('git', ['reset', '-q', 'leak.js'], { cwd: dir });
  fs.rmSync(path.join(dir, 'leak.js'));
  fs.writeFileSync(path.join(dir, '.gitignore'), '.env\n');
  // secscan-ignore: slack-bot-token synthetic value assembled at runtime for the gitignore test
  fs.writeFileSync(path.join(dir, '.env'), 'SLACK_TOKEN=xoxb-123456789012-1234567890123-' + rand(24) + '\n');
  const local = JSON.parse(secscan(['scan', '--only', 'gitleaks', '--format', 'json', '--quiet', '--fail-on', 'medium'], dir).stdout);
  const env = local.findings.find((f) => f.file === '.env');
  assert.ok(env, 'gitignored .env is still reported');
  assert.equal(env.severity, 'low');
  assert.equal(env.extra.gitignored, true);
  assert.equal(local.ok, true, 'gitignored secret does not block');
  fs.rmSync(dir, { recursive: true, force: true });
});

test('cli: python fixture is detected and scanned', () => {
  const d = secscan(['detect', PY_FIXTURE], PY_FIXTURE);
  assert.equal(d.status, 0);
  assert.match(d.stdout, /python \(1\)/);
  assert.match(d.stdout, /python x1/);
  if (!resolveTool('osv-scanner').path) return;
  const r = secscan(['scan', PY_FIXTURE, '--no-baseline', '--format', 'json', '--quiet', '--only', 'osv-scanner,pip-audit,sighthound'], PY_FIXTURE);
  const j = JSON.parse(r.stdout);
  const yaml = j.findings.find((f) => f.category === 'supply-chain' && f.extra.package === 'pyyaml');
  assert.ok(yaml, 'pyyaml advisory found from requirements.txt');
  assert.equal(yaml.file, 'requirements.txt');
  if (resolveTool('sighthound').path) assert.ok(j.findings.some((f) => f.ruleId === 'sighthound/command-injection' && f.file === 'app.py'));
});
