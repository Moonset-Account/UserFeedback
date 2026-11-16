import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { run, relPosix, sha256 } from '../util.js';
import { resolveTool } from '../tools.js';

export const name = 'gitleaks';
export const category = 'secrets';

export function applies() { return true; }

export function available() {
  const r = resolveTool('gitleaks');
  return r.path ? { ok: true, bin: r.path } : { ok: false, reason: r.reason };
}

export function version(bin) {
  const r = run(bin, ['version']);
  return ((r.stdout + r.stderr).match(/\d+\.\d+\.\d+/) || [null])[0];
}

/**
 * mode.hook: staged changes only. history=true: full git history. Otherwise: working tree.
 */
export function scan({ root, bin, config, mode, detect }) {
  const report = path.join(os.tmpdir(), `secscan-gitleaks-${process.pid}.json`);
  const common = ['--report-format', 'json', '--report-path', report, '--exit-code', '0', '--no-banner', '--no-color'];
  let args;
  const wantHistory = mode === 'ci' ? config.gitleaks?.history?.ci : config.gitleaks?.history?.local;
  if (mode === 'hook' && detect.hasGit) args = ['git', '--pre-commit', '--staged', ...common, '.'];
  else if (wantHistory && detect.hasGit) args = ['git', ...common, '.'];
  else args = ['dir', ...common, '.'];

  const r = run(bin, args, { cwd: root });
  if (r.error) return { findings: [], error: r.error.message };
  let arr = [];
  try {
    if (fs.existsSync(report)) { arr = JSON.parse(fs.readFileSync(report, 'utf8') || '[]'); fs.rmSync(report, { force: true }); }
  } catch (e) { return { findings: [], error: `bad report: ${e.message}` }; }
  if (r.status !== 0) return { findings: parse(arr, root), error: r.stderr.trim().slice(0, 300) || `exit ${r.status}` };
  return { findings: parse(arr, root) };
}

function redact(s) {
  const t = String(s || '');
  if (t.length <= 8) return '****';
  return t.slice(0, 4) + '…' + t.slice(-2);
}

export function parse(arr, root) {
  return (arr || []).map((l) => ({
    tool: name,
    category,
    ruleId: `gitleaks/${l.RuleID}`,
    severity: 'high',
    message: `${l.Description || l.RuleID} in ${relPosix(root, l.File)}${l.Commit ? ` (commit ${String(l.Commit).slice(0, 8)})` : ''}: ${redact(l.Secret)}`,
    file: relPosix(root, l.File),
    line: l.StartLine || null,
    endLine: l.EndLine || null,
    snippet: null, // never carry the secret
    cwe: 'CWE-798',
    extra: {
      secretHash: sha256(String(l.Secret || '')).slice(0, 16),
      commit: l.Commit || null,
      author: l.Author || null,
      entropy: l.Entropy ?? null,
      redacted: redact(l.Secret),
    },
  }));
}
