import path from 'node:path';
import { readJson, writeJson, exists, nowIso, gitHead } from './util.js';
import { BASELINE_FILE } from './config.js';

/**
 * Baseline file: { version, createdAt, updatedAt, commit, tools: {name: version},
 *   findings: { [fingerprint]: { tool, ruleId, severity, file, line, message, addedAt } } }
 */
export function loadBaseline(root) {
  const file = path.join(root, BASELINE_FILE);
  if (!exists(file)) return { version: 1, createdAt: null, updatedAt: null, commit: null, tools: {}, findings: {} };
  const b = readJson(file);
  b.findings = b.findings || {};
  return b;
}

export function saveBaseline(root, baseline) {
  writeJson(path.join(root, BASELINE_FILE), baseline);
}

/** Mark findings that are already in the baseline. Mutates status. */
export function applyBaseline(findings, baseline) {
  for (const f of findings) {
    if (f.status === 'new' && baseline.findings[f.fingerprint]) f.status = 'baseline';
  }
  return findings;
}

/** Replace the baseline with the current (non-suppressed) findings. */
export function buildBaseline(root, findings, toolVersions, previous = null) {
  const now = nowIso();
  const out = {
    version: 1,
    createdAt: previous?.createdAt || now,
    updatedAt: now,
    commit: gitHead(root),
    tools: toolVersions,
    findings: {},
  };
  for (const f of findings) {
    if (f.status === 'suppressed') continue;
    const prev = previous?.findings?.[f.fingerprint];
    out.findings[f.fingerprint] = {
      tool: f.tool, ruleId: f.ruleId, severity: f.severity, category: f.category,
      file: f.file, line: f.line, message: String(f.message || '').slice(0, 200),
      addedAt: prev?.addedAt || now,
    };
  }
  return out;
}

/** Findings in the baseline that no longer occur (fixed or moved). */
export function resolvedSince(findings, baseline) {
  const seen = new Set(findings.map((f) => f.fingerprint));
  return Object.entries(baseline.findings).filter(([fp]) => !seen.has(fp)).map(([fp, v]) => ({ fingerprint: fp, ...v }));
}
