import { run, relPosix, normalizeSeverity } from '../util.js';
import { resolveTool } from '../tools.js';

const SAST_LANGS = ['javascript', 'typescript', 'python', 'java', 'go', 'ruby', 'php', 'csharp', 'html'];

export const name = 'sighthound';
export const category = 'sast';

export function applies(detect) {
  return SAST_LANGS.some((l) => detect.languages.has(l));
}

export function available() {
  const r = resolveTool('sighthound');
  return r.path ? { ok: true, bin: r.path } : { ok: false, reason: r.reason };
}

export function version(bin) {
  const r = run(bin, ['--version']);
  return (r.stdout.match(/\d+\.\d+(?:\.\d+)?/) || [null])[0];
}

export function scan({ root, bin }) {
  const r = run(bin, ['-o', 'json', '.'], { cwd: root });
  if (r.error) return { findings: [], error: r.error.message };
  const text = r.stdout.trim();
  const start = text.indexOf('[');
  if (start < 0) return { findings: [], error: r.stderr.trim() || 'no JSON output' };
  let arr;
  try { arr = JSON.parse(text.slice(start)); } catch (e) { return { findings: [], error: `bad JSON: ${e.message}` }; }
  return { findings: parse(arr, root) };
}

const DOWNGRADE = { critical: 'high', high: 'medium', medium: 'low', low: 'info', info: 'info' };

/**
 * A finding with a source is a taint flow (source -> sink). One without a source is a
 * sink-only pattern match, which is where nearly all of Sighthound's noise lives, so it
 * drops one severity notch and is marked extra.taintFlow=false for the triager.
 */
export function parse(arr, root) {
  return (arr || []).map((v) => ({
    tool: name,
    category,
    ruleId: `sighthound/${slug(v.finding_type || 'finding')}`,
    severity: v.source_info?.source_type ? normalizeSeverity(v.severity) : DOWNGRADE[normalizeSeverity(v.severity)],
    message: `${v.finding_type || 'Finding'}: ${v.description || ''}`.trim() +
      (v.source_info?.source_type ? ` (source: ${v.source_info.source_type})` : ''),
    file: relPosix(root, v.file),
    line: v.line || null,
    endLine: v.end_line || null,
    snippet: v.snippet || null,
    cwe: v.cwe_id ? String(v.cwe_id).toUpperCase() : null,
    extra: { confidence: v.confidence || null, function: v.function || null, taintFlow: !!v.source_info?.source_type, source: v.source_info?.source_type || null },
  }));
}

function slug(s) { return String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''); }
