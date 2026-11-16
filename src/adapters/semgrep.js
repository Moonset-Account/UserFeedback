import { run, relPosix } from '../util.js';
import { resolveTool } from '../tools.js';

const SAST_LANGS = ['javascript', 'typescript', 'python', 'java', 'go', 'ruby', 'php', 'csharp', 'html'];

export const name = 'semgrep';
export const category = 'sast';

export function applies(detect) {
  return SAST_LANGS.some((l) => detect.languages.has(l));
}

export function available() {
  const r = resolveTool('semgrep');
  return r.path ? { ok: true, bin: r.path } : { ok: false, reason: r.reason };
}

export function version(bin) {
  const r = run(bin, ['--version']);
  return (r.stdout.match(/\d+\.\d+\.\d+/) || [null])[0];
}

export function scan({ root, bin, config }) {
  const configs = config.semgrep?.configs?.length ? config.semgrep.configs : ['p/default'];
  const args = ['scan', '--json', '--metrics=off', '--quiet', '--disable-version-check'];
  for (const c of configs) args.push('--config', c);
  for (const g of config.exclude || []) args.push('--exclude', g.replace(/^\*\*\//, ''));
  args.push(...(config.semgrep?.extraArgs || []), '.');
  const r = run(bin, args, { cwd: root, env: { ...process.env, SEMGREP_SEND_METRICS: 'off' } });
  if (r.error) return { findings: [], error: r.error.message };
  const text = r.stdout.trim();
  const start = text.indexOf('{');
  if (start < 0) return { findings: [], error: r.stderr.trim().slice(0, 500) || 'no JSON output' };
  let json;
  try { json = JSON.parse(text.slice(start)); } catch (e) { return { findings: [], error: `bad JSON: ${e.message}` }; }
  const errors = (json.errors || []).filter((e) => e.level === 'error').map((e) => e.message || e.type);
  return { findings: parse(json, root), error: errors.length ? errors.slice(0, 3).join('; ') : undefined };
}

function sev(extra) {
  const s = String(extra?.severity || '').toUpperCase();
  const conf = String(extra?.metadata?.confidence || '').toUpperCase();
  const impact = String(extra?.metadata?.impact || '').toUpperCase();
  if (s === 'ERROR') return impact === 'HIGH' && conf === 'HIGH' ? 'critical' : 'high';
  if (s === 'WARNING') return 'medium';
  return 'low';
}

export function parse(json, root) {
  return (json.results || []).map((r) => {
    const cwe = r.extra?.metadata?.cwe;
    const cweText = Array.isArray(cwe) ? cwe[0] : cwe;
    return {
      tool: name,
      category,
      ruleId: `semgrep/${r.check_id}`,
      severity: sev(r.extra),
      message: r.extra?.message || r.check_id,
      file: relPosix(root, r.path),
      line: r.start?.line || null,
      endLine: r.end?.line || null,
      snippet: r.extra?.lines || null,
      cwe: cweText ? (String(cweText).match(/CWE-\d+/i) || [null])[0]?.toUpperCase() || null : null,
      extra: {
        confidence: r.extra?.metadata?.confidence || null,
        likelihood: r.extra?.metadata?.likelihood || null,
        impact: r.extra?.metadata?.impact || null,
        owasp: r.extra?.metadata?.owasp || null,
        references: r.extra?.metadata?.references?.slice(0, 3) || null,
      },
    };
  });
}
