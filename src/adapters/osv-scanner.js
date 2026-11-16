import { run, relPosix, cvssToSeverity, normalizeSeverity } from '../util.js';
import { resolveTool } from '../tools.js';

export const name = 'osv-scanner';
export const category = 'supply-chain';

export function applies(detect) {
  return detect.anyLockfile;
}

export function available() {
  const r = resolveTool('osv-scanner');
  return r.path ? { ok: true, bin: r.path } : { ok: false, reason: r.reason };
}

export function version(bin) {
  const r = run(bin, ['--version']);
  return ((r.stdout + r.stderr).match(/\d+\.\d+\.\d+/) || [null])[0];
}

export function scan({ root, bin }) {
  // Exit code 1 means "vulnerabilities found", not an error.
  const r = run(bin, ['scan', 'source', '--format', 'json', '--recursive', '.'], { cwd: root });
  if (r.error) return { findings: [], error: r.error.message };
  const text = r.stdout.trim();
  const start = text.indexOf('{');
  if (start < 0) {
    if (r.status === 0 || /no package sources found/i.test(r.stderr)) return { findings: [] };
    return { findings: [], error: r.stderr.trim().slice(0, 300) || `exit ${r.status}` };
  }
  let json;
  try { json = JSON.parse(text.slice(start)); } catch (e) { return { findings: [], error: `bad JSON: ${e.message}` }; }
  return { findings: parse(json, root), error: r.status > 1 ? r.stderr.trim().slice(0, 300) : undefined };
}

function severityOf(vuln, groups) {
  const g = groups.find((gr) => (gr.ids || []).includes(vuln.id) || (gr.aliases || []).includes(vuln.id));
  if (g?.max_severity) return cvssToSeverity(g.max_severity);
  const dbs = vuln.database_specific?.severity;
  if (dbs) return normalizeSeverity(dbs);
  const cvss = (vuln.severity || []).find((s) => /CVSS/i.test(s.type));
  if (cvss?.score && /^\d/.test(cvss.score)) return cvssToSeverity(cvss.score);
  return 'medium';
}

function cmpVer(a, b) {
  const pa = String(a).split(/[.+-]/).map((x) => (/^\d+$/.test(x) ? Number(x) : x));
  const pb = String(b).split(/[.+-]/).map((x) => (/^\d+$/.test(x) ? Number(x) : x));
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] ?? 0, y = pb[i] ?? 0;
    if (x === y) continue;
    if (typeof x === 'number' && typeof y === 'number') return x - y;
    return String(x) < String(y) ? -1 : 1;
  }
  return 0;
}

/** The fixed version for the range that actually contains the installed version. */
function fixedVersion(vuln, pkg) {
  let fallback = null;
  for (const a of vuln.affected || []) {
    if (a.package?.name !== pkg.name) continue;
    for (const r of a.ranges || []) {
      let introduced = '0', fixed = null;
      for (const e of r.events || []) {
        if (e.introduced !== undefined) introduced = e.introduced;
        if (e.fixed !== undefined) fixed = e.fixed;
      }
      if (!fixed) continue;
      fallback = fallback || fixed;
      if (pkg.version && cmpVer(pkg.version, introduced) >= 0 && cmpVer(pkg.version, fixed) < 0) return fixed;
    }
  }
  return fallback;
}

export function parse(json, root) {
  const out = [];
  for (const res of json.results || []) {
    const file = relPosix(root, res.source?.path);
    for (const p of res.packages || []) {
      const pkg = p.package || {};
      const groups = p.groups || [];
      for (const v of p.vulnerabilities || []) {
        const fixed = fixedVersion(v, pkg);
        out.push({
          tool: name,
          category,
          ruleId: `osv/${v.id}`,
          severity: severityOf(v, groups),
          message: `${pkg.name}@${pkg.version} (${pkg.ecosystem}): ${v.summary || v.id}${fixed ? `. Fixed in ${fixed}` : ''}`,
          file,
          line: null,
          snippet: null,
          cwe: null,
          extra: {
            package: pkg.name, version: pkg.version, ecosystem: pkg.ecosystem,
            aliases: v.aliases || [], fixedIn: fixed, advisory: `https://osv.dev/vulnerability/${v.id}`,
            title: v.summary || null,
          },
        });
      }
    }
  }
  return out;
}
