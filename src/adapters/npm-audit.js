import path from 'node:path';
import { run, relPosix, normalizeSeverity, IS_WIN } from '../util.js';

export const name = 'npm-audit';
export const category = 'supply-chain';

export function applies(detect) {
  return detect.lockfiles.npm.length > 0;
}

export function available() {
  const r = run(IS_WIN ? 'npm.cmd' : 'npm', ['--version'], { shell: IS_WIN });
  return r.status === 0 ? { ok: true, bin: IS_WIN ? 'npm.cmd' : 'npm' } : { ok: false, reason: 'npm not on PATH' };
}

export function version(bin) {
  const r = run(bin, ['--version'], { shell: IS_WIN });
  return r.stdout.trim() || null;
}

export function scan({ root, bin, detect }) {
  const findings = [];
  const errors = [];
  for (const lock of detect.lockfiles.npm) {
    const dir = path.dirname(lock);
    // No user-controlled args; shell only so npm.cmd resolves on Windows.
    const r = run(bin, ['audit', '--json', '--audit-level=info'], { cwd: dir, shell: IS_WIN });
    const text = r.stdout.trim();
    const start = text.indexOf('{');
    if (start < 0) { errors.push(`${relPosix(root, lock)}: ${r.stderr.trim().slice(0, 200) || 'no output'}`); continue; }
    let json;
    try { json = JSON.parse(text.slice(start)); } catch (e) { errors.push(`${relPosix(root, lock)}: bad JSON`); continue; }
    if (json.error) { errors.push(`${relPosix(root, lock)}: ${json.error.summary || json.error.code}`); continue; }
    findings.push(...parse(json, relPosix(root, lock)));
  }
  return { findings, error: errors.length ? errors.join('; ') : undefined };
}

/** npm audit v7+ JSON: vulnerabilities[name] = { severity, via: [advisory|name], range, fixAvailable, isDirect }. */
export function parse(json, lockfileRel) {
  const out = [];
  const seen = new Set();
  for (const [pkg, v] of Object.entries(json.vulnerabilities || {})) {
    for (const via of v.via || []) {
      if (typeof via !== 'object') continue; // transitive pointer, the advisory is reported on its own package
      const id = via.url?.match(/GHSA-[\w-]+/)?.[0] || `npm-${via.source}`;
      const key = `${id}|${pkg}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const fix = v.fixAvailable === true ? 'fix available' :
        v.fixAvailable && typeof v.fixAvailable === 'object' ? `fix: ${v.fixAvailable.name}@${v.fixAvailable.version}${v.fixAvailable.isSemVerMajor ? ' (major)' : ''}` :
        'no fix';
      out.push({
        tool: name,
        category,
        ruleId: `npm-audit/${id}`,
        severity: normalizeSeverity(via.severity || v.severity),
        message: `${pkg} ${via.range || v.range || ''}: ${via.title || id}. ${fix}${v.isDirect ? ' (direct dependency)' : ' (transitive)'}`,
        file: lockfileRel,
        line: null,
        snippet: null,
        cwe: Array.isArray(via.cwe) && via.cwe.length ? String(via.cwe[0]).toUpperCase() : null,
        extra: {
          package: pkg,
          version: via.range || v.range || null,
          advisory: via.url || null,
          title: via.title || null,
          direct: !!v.isDirect,
          fixAvailable: v.fixAvailable ?? null,
          cvss: via.cvss?.score ?? null,
        },
      });
    }
  }
  return out;
}
