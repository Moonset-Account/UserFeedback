import path from 'node:path';
import { run, relPosix, which } from '../util.js';
import { toolsDir } from '../tools.js';

export const name = 'pip-audit';
export const category = 'supply-chain';

/** Runs on pinned requirements files. Poetry, uv, PDM and Pipfile locks are covered by osv-scanner. */
export function applies(detect) {
  return detect.lockfiles.pythonRequirements?.length > 0;
}

export function available() {
  const bin = which('pip-audit', [toolsDir()]);
  if (bin) return { ok: true, bin };
  // fall back to `python -m pip_audit` when the module is installed but the script is not on PATH
  for (const py of ['python3', 'python']) {
    const r = run(py, ['-m', 'pip_audit', '--version']);
    if (r.status === 0) return { ok: true, bin: py, module: true };
  }
  return { ok: false, reason: 'not installed; pip install pip-audit==2.10.1 (Linux/macOS/WSL/Windows) or put pip-audit on PATH' };
}

export function version(bin) {
  const r = bin.startsWith('python') ? run(bin, ['-m', 'pip_audit', '--version']) : run(bin, ['--version']);
  return ((r.stdout + r.stderr).match(/\d+\.\d+\.\d+/) || [null])[0];
}

export function scan({ root, bin, detect }) {
  const findings = [];
  const errors = [];
  for (const req of detect.lockfiles.pythonRequirements) {
    const rel = relPosix(root, req);
    const base = bin.startsWith('python') ? [bin, ['-m', 'pip_audit']] : [bin, []];
    // --no-deps: audit exactly what the file pins; resolving would need a build environment and network.
    const args = [...base[1], '-r', req, '--no-deps', '--format', 'json', '--progress-spinner', 'off', '--disable-pip'];
    const r = run(base[0], args, { cwd: path.dirname(req) });
    if (r.error) { errors.push(`${rel}: ${r.error.message}`); continue; }
    const text = r.stdout.trim();
    const start = text.indexOf('{');
    if (start < 0) { errors.push(`${rel}: ${r.stderr.trim().slice(0, 200) || 'no output'}`); continue; }
    let json;
    try { json = JSON.parse(text.slice(start)); } catch { errors.push(`${rel}: bad JSON`); continue; }
    findings.push(...parse(json, rel));
    if (r.status > 1) errors.push(`${rel}: exit ${r.status} ${r.stderr.trim().slice(0, 200)}`);
  }
  return { findings, error: errors.length ? errors.join('; ') : undefined };
}

/** pip-audit JSON: { dependencies: [{ name, version, vulns: [{ id, fix_versions, aliases, description }] }] } */
export function parse(json, requirementsRel) {
  const out = [];
  for (const dep of json.dependencies || []) {
    if (dep.skip_reason) continue;
    for (const v of dep.vulns || []) {
      const ghsa = [v.id, ...(v.aliases || [])].find((a) => /^GHSA-/i.test(a));
      const id = ghsa || v.id;
      const fixed = (v.fix_versions || [])[0] || null;
      out.push({
        tool: name,
        category,
        ruleId: `pip-audit/${id}`,
        severity: 'medium', // pip-audit carries no severity; the osv row for the same advisory wins in dedupe and brings CVSS
        message: `${dep.name}@${dep.version} (PyPI): ${(v.description || v.id).split('\n')[0].slice(0, 200)}${fixed ? `. Fixed in ${fixed}` : ''}`,
        file: requirementsRel,
        line: null,
        snippet: null,
        cwe: null,
        extra: {
          package: dep.name, version: dep.version, ecosystem: 'PyPI',
          aliases: [v.id, ...(v.aliases || [])].filter((a) => a !== id), fixedIn: fixed,
          advisory: /^GHSA-/i.test(id) ? `https://github.com/advisories/${id}` : `https://osv.dev/vulnerability/${v.id}`,
          title: (v.description || '').split('\n')[0].slice(0, 120) || null,
        },
      });
    }
  }
  return out;
}
