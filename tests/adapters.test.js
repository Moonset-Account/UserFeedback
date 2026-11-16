import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as sighthound from '../src/adapters/sighthound.js';
import * as semgrep from '../src/adapters/semgrep.js';
import * as npmAudit from '../src/adapters/npm-audit.js';
import * as osv from '../src/adapters/osv-scanner.js';
import * as gitleaks from '../src/adapters/gitleaks.js';
import * as pipAudit from '../src/adapters/pip-audit.js';
import { finalize } from '../src/findings.js';
import { dedupeSupplyChain } from '../src/supplychain.js';
import { toSarif } from '../src/sarif.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const sample = (n) => JSON.parse(fs.readFileSync(path.join(HERE, 'samples', n), 'utf8'));
const ROOT = path.join(HERE, '..', 'fixtures', 'node-vuln');

test('sighthound: taint flows keep severity, sink-only hits drop a notch', () => {
  const out = sighthound.parse(sample('sighthound.json'), ROOT);
  assert.ok(out.length >= 5);
  const cmd = out.find((f) => f.ruleId === 'sighthound/command-injection');
  assert.equal(cmd.severity, 'critical');
  assert.equal(cmd.extra.taintFlow, true);
  assert.equal(cmd.file, 'app.js');
  assert.equal(cmd.cwe, 'CWE-78');
  const sinkOnly = out.filter((f) => !f.extra.taintFlow);
  for (const f of sinkOnly) assert.ok(['medium', 'low', 'info'].includes(f.severity), `${f.ruleId} should be downgraded, got ${f.severity}`);
});

test('npm-audit: one finding per advisory, transitive pointers skipped, fix hint present', () => {
  const out = npmAudit.parse(sample('npm-audit.json'), 'package-lock.json');
  assert.ok(out.length >= 10);
  const minimist = out.find((f) => f.extra.package === 'minimist');
  assert.ok(minimist, 'minimist advisory');
  assert.equal(minimist.severity, 'critical');
  assert.match(minimist.ruleId, /^npm-audit\/GHSA-/);
  assert.equal(minimist.extra.direct, true);
  assert.equal(minimist.category, 'supply-chain');
  const ids = out.map((f) => `${f.ruleId}|${f.extra.package}`);
  assert.equal(new Set(ids).size, ids.length, 'no duplicate advisory+package rows');
});

test('osv-scanner: fixed version comes from the range containing the installed version', () => {
  const out = osv.parse(sample('osv-scanner.json'), ROOT);
  assert.ok(out.length >= 3);
  for (const f of out) {
    assert.equal(f.file, 'package-lock.json');
    assert.ok(f.extra.package && f.extra.version);
    assert.match(f.ruleId, /^osv\//);
  }
  const bp = out.find((f) => f.extra.package === 'body-parser');
  assert.ok(bp.extra.fixedIn, 'has fixedIn');
  assert.ok(bp.extra.fixedIn.startsWith('1.'), `fixedIn should be on the 1.x line, got ${bp.extra.fixedIn}`);
});

test('gitleaks: secret never appears in the finding, hash does', () => {
  const out = gitleaks.parse(sample('gitleaks.json'), ROOT);
  assert.equal(out.length, 1);
  const f = out[0];
  assert.equal(f.ruleId, 'gitleaks/github-pat');
  assert.equal(f.file, 'config.js');
  assert.equal(f.line, 2);
  assert.equal(f.snippet, null);
  assert.ok(!JSON.stringify(f).includes('ghp_SAMPLEsampleSAMPLE'), 'secret leaked into finding');
  assert.equal(f.extra.secretHash.length, 16);
  assert.equal(f.cwe, 'CWE-798');
});

test('semgrep: severity mapping and cwe extraction', () => {
  const json = { results: [
    { check_id: 'javascript.express.security.injection.tainted-sql-string', path: 'app.js', start: { line: 19 }, end: { line: 19 },
      extra: { message: 'SQLi', severity: 'ERROR', lines: 'db.query("..." + req.query.id)', metadata: { cwe: ['CWE-89: Improper Neutralization'], confidence: 'HIGH', impact: 'HIGH' } } },
    { check_id: 'x.y.warn', path: 'a.js', start: { line: 1 }, end: { line: 1 }, extra: { message: 'w', severity: 'WARNING', metadata: { cwe: 'CWE-20' } } },
    { check_id: 'x.y.info', path: 'a.js', start: { line: 2 }, end: { line: 2 }, extra: { message: 'i', severity: 'INFO', metadata: {} } },
  ] };
  const out = semgrep.parse(json, ROOT);
  assert.equal(out[0].severity, 'critical');
  assert.equal(out[0].cwe, 'CWE-89');
  assert.equal(out[0].ruleId, 'semgrep/javascript.express.security.injection.tainted-sql-string');
  assert.equal(out[1].severity, 'medium');
  assert.equal(out[1].cwe, 'CWE-20');
  assert.equal(out[2].severity, 'low');
  assert.equal(out[2].cwe, null);
});

test('supply-chain dedupe merges npm-audit and osv rows for the same advisory', () => {
  const a = npmAudit.parse(sample('npm-audit.json'), 'package-lock.json').map(finalize);
  const b = osv.parse(sample('osv-scanner.json'), ROOT).map(finalize);
  const merged = dedupeSupplyChain([...a, ...b], ROOT);
  const bodyParser = merged.filter((f) => f.extra.package === 'body-parser' && /GHSA-qwcr-r2fm-qrc7/.test(f.ruleId));
  assert.equal(bodyParser.length, 1, 'one row for the shared advisory');
  assert.equal(bodyParser[0].tool, 'osv-scanner');
  assert.deepEqual(bodyParser[0].extra.alsoReportedBy, ['npm-audit']);
  assert.equal(typeof bodyParser[0].extra.dev, 'boolean');
});

test('sarif output has one run per tool, fingerprints and locations', () => {
  const findings = [...sighthound.parse(sample('sighthound.json'), ROOT), ...gitleaks.parse(sample('gitleaks.json'), ROOT)].map(finalize);
  const sarif = toSarif(findings, { toolVersions: { sighthound: '1.0' }, secscanVersion: '0.1.0' });
  assert.equal(sarif.version, '2.1.0');
  assert.deepEqual(sarif.runs.map((r) => r.tool.driver.name).sort(), ['gitleaks', 'sighthound']);
  const res = sarif.runs.find((r) => r.tool.driver.name === 'sighthound').results[0];
  assert.ok(res.partialFingerprints['secscan/v1']);
  assert.equal(res.locations[0].physicalLocation.artifactLocation.uri, 'app.js');
  assert.ok(res.locations[0].physicalLocation.region.startLine > 0);
  const secret = sarif.runs.find((r) => r.tool.driver.name === 'gitleaks').results[0];
  assert.equal(secret.locations[0].physicalLocation.region.snippet, undefined);
});

test('pip-audit: one row per advisory, GHSA preferred as id, fix version carried', () => {
  const out = pipAudit.parse(sample('pip-audit.json'), 'requirements.txt');
  assert.ok(out.length >= 20);
  const yaml = out.find((f) => f.extra.package === 'pyyaml');
  assert.ok(yaml);
  assert.equal(yaml.ruleId, 'pip-audit/GHSA-8q59-q68h-6hv4');
  assert.equal(yaml.extra.fixedIn, '5.4');
  assert.equal(yaml.file, 'requirements.txt');
  assert.ok(yaml.extra.aliases.includes('PYSEC-2021-142'));
  assert.equal(yaml.category, 'supply-chain');
});

test('supply-chain dedupe merges pip-audit into the osv row and borrows its title', () => {
  const osvRows = [finalize({ tool: 'osv-scanner', category: 'supply-chain', ruleId: 'osv/PYSEC-2021-142', severity: 'critical', message: 'pyyaml@5.3.1 (PyPI): PYSEC-2021-142. Fixed in 5.4', file: 'requirements.txt', extra: { package: 'pyyaml', version: '5.3.1', aliases: ['CVE-2020-14343', 'GHSA-8q59-q68h-6hv4'], fixedIn: '5.4', title: null } })];
  const pa = pipAudit.parse(sample('pip-audit.json'), 'requirements.txt').map(finalize).filter((f) => f.extra.package === 'pyyaml' && f.ruleId.endsWith('GHSA-8q59-q68h-6hv4'));
  const merged = dedupeSupplyChain([...osvRows, ...pa], ROOT);
  const rows = merged.filter((f) => f.extra.package === 'pyyaml');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].tool, 'osv-scanner');
  assert.equal(rows[0].severity, 'critical');
  assert.deepEqual(rows[0].extra.alsoReportedBy, ['pip-audit']);
  assert.match(rows[0].message, /PyYAML library/);
});
