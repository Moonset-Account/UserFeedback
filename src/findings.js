import fs from 'node:fs';
import path from 'node:path';
import { sha256, matchesAny, SEVERITY_ORDER } from './util.js';

/**
 * Unified finding:
 * { tool, ruleId, category: 'sast'|'secrets'|'supply-chain', severity, message,
 *   file (repo-relative posix) | null, line | null, endLine | null, snippet | null,
 *   cwe | null, extra: {}, fingerprint, status: 'new'|'baseline'|'suppressed', suppressedBy? }
 */

function normalizeSnippet(s) {
  return String(s || '').replace(/\s+/g, ' ').trim().slice(0, 400);
}

/**
 * Stable identity for a finding across edits elsewhere in the file:
 * SAST: tool + rule + file + normalized snippet (falls back to line when no snippet).
 * Secrets: tool + rule + file + hash of the secret (never the secret itself).
 * Supply chain: tool + vuln id + package + version + lockfile.
 */
export function fingerprint(f) {
  let key;
  if (f.category === 'supply-chain') {
    key = ['sc', f.tool, f.ruleId, f.extra?.package, f.extra?.version, f.file].join('|');
  } else if (f.category === 'secrets') {
    key = ['sec', f.tool, f.ruleId, f.file, f.extra?.secretHash || f.line].join('|');
  } else {
    const body = f.snippet ? normalizeSnippet(f.snippet) : `L${f.line}`;
    key = ['sast', f.tool, f.ruleId, f.file, body].join('|');
  }
  return sha256(key).slice(0, 32);
}

export function finalize(f) {
  f.severity = f.severity || 'medium';
  f.extra = f.extra || {};
  f.fingerprint = fingerprint(f);
  f.status = 'new';
  return f;
}

const INLINE_RE = /secscan-ignore(?::\s*([\w./:-]+))?(?:\s+(.*))?/;

/** Inline suppression: a `secscan-ignore[: rule-id] [reason]` comment on the finding line or the line above. */
export function inlineSuppression(root, f, cache) {
  if (!f.file || !f.line) return null;
  const abs = path.join(root, f.file);
  let lines = cache.get(abs);
  if (lines === undefined) {
    try { lines = fs.readFileSync(abs, 'utf8').split(/\r?\n/); } catch { lines = null; }
    cache.set(abs, lines);
  }
  if (!lines) return null;
  for (const idx of [f.line - 1, f.line - 2]) {
    const text = lines[idx];
    if (!text) continue;
    const m = text.match(INLINE_RE);
    if (!m) continue;
    const rule = m[1];
    if (!rule || rule === f.ruleId || rule === f.tool || f.ruleId.endsWith('/' + rule)) {
      return { kind: 'inline', reason: (m[2] || '').trim() || 'inline secscan-ignore', line: idx + 1 };
    }
  }
  return null;
}

/** Config suppression: matches on tool, rule (exact or suffix), and path glob; honours expiry. */
export function configSuppression(f, rules = [], today = new Date()) {
  for (const s of rules) {
    if (s.expires && new Date(s.expires) < today) continue;
    if (s.tool && s.tool !== f.tool) continue;
    if (s.rule && !(s.rule === f.ruleId || f.ruleId.endsWith('/' + s.rule))) continue;
    if (s.path && !(f.file && matchesAny(f.file, [s.path]))) continue;
    if (!s.tool && !s.rule && !s.path) continue; // an empty rule would suppress everything
    return { kind: 'config', reason: s.reason || 'suppressed by .secscan.json', expires: s.expires || null };
  }
  return null;
}

export function applyExcludes(findings, excludeGlobs) {
  if (!excludeGlobs?.length) return findings;
  return findings.filter((f) => !(f.file && matchesAny(f.file, excludeGlobs)));
}

export function meetsThreshold(sev, threshold) {
  if (!threshold || threshold === 'none') return false;
  return (SEVERITY_ORDER[sev] ?? 0) >= (SEVERITY_ORDER[threshold] ?? 99);
}

export function sortFindings(a, b) {
  const d = (SEVERITY_ORDER[b.severity] ?? 0) - (SEVERITY_ORDER[a.severity] ?? 0);
  if (d) return d;
  return (a.file || '').localeCompare(b.file || '') || (a.line || 0) - (b.line || 0);
}
