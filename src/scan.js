import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { readJson, gitChangedFiles, nowIso, relPosix, matchesAny, gitIgnored } from './util.js';
import { loadConfig } from './config.js';
import { detectRepo } from './detect.js';
import { selectAdapters } from './adapters/index.js';
import { finalize, inlineSuppression, configSuppression, applyExcludes, meetsThreshold, sortFindings } from './findings.js';
import { loadBaseline, applyBaseline, resolvedSince } from './baseline.js';
import { npmLockDiff, registryMeta } from './deps.js';
import { dedupeSupplyChain } from './supplychain.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const SECSCAN_VERSION = readJson(path.join(HERE, '..', 'package.json')).version;

/**
 * Run a scan. mode: 'local' | 'ci' | 'hook'.
 * changed: null (whole tree) | 'staged' | '<git ref>' to restrict SAST/secrets findings to changed files.
 */
export async function runScan(root, {
  mode = 'local', only = null, changed = null, useBaseline = true, failOn = null, log = () => {},
} = {}) {
  const startedAt = Date.now();
  const config = loadConfig(root);
  // never report on secscan's own checkout or output inside a scanned workspace (CI), whatever the config says
  config.exclude = [...new Set([...(config.exclude || []), '**/.secscan-tool/**', '**/secscan-out/**', '**/.git/**', '**/node_modules/**', '**/.venv/**', '**/venv/**', '**/__pycache__/**'])];
  const detect = detectRepo(root);
  // excluded paths never feed the supply-chain adapters either (e.g. a vulnerable fixture lockfile)
  for (const k of Object.keys(detect.lockfiles)) {
    // secscan-ignore: object-property-injection keys are the fixed ecosystem names from detect.js
    detect.lockfiles[k] = detect.lockfiles[k].filter((abs) => !matchesAny(relPosix(root, abs), config.exclude || []));
  }
  detect.anyLockfile = Object.values(detect.lockfiles).some((a) => a.length > 0);
  const selection = selectAdapters(config, detect, { only, mode });

  let changedFiles = null;
  if (changed) {
    changedFiles = new Set(gitChangedFiles(root, changed === 'staged' ? null : changed));
    log(`[secscan] restricting to ${changedFiles.size} changed file(s)`);
  }
  const lockfileChanged = !changedFiles || [...changedFiles].some((f) => /(^|\/)(package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock|requirements\.txt|poetry\.lock|go\.mod|Cargo\.lock|Gemfile\.lock|composer\.lock)$/.test(f));

  const toolRuns = [];
  let findings = [];
  for (const row of selection) {
    if (!row.enabled) { toolRuns.push({ tool: row.name, ran: false, reason: row.reason }); continue; }
    if (row.category === 'supply-chain' && !lockfileChanged) { toolRuns.push({ tool: row.name, ran: false, reason: 'no lockfile changed' }); continue; }
    const t0 = Date.now();
    log(`[secscan] ${row.name} ${row.version || ''}`.trim());
    let res;
    try { res = row.adapter.scan({ root, bin: row.bin, config, detect, mode }); }
    catch (e) { res = { findings: [], error: e.message }; }
    const list = (res.findings || []).map(finalize);
    findings.push(...list);
    toolRuns.push({ tool: row.name, ran: true, version: row.version, ms: Date.now() - t0, count: list.length, error: res.error || null });
  }

  findings = dedupeSupplyChain(applyExcludes(findings, config.exclude), root);
  markGitignoredSecrets(findings, root, detect);
  if (changedFiles) {
    findings = findings.filter((f) => f.category === 'supply-chain' || (f.file && changedFiles.has(f.file)));
  }

  // suppressions
  const cache = new Map();
  for (const f of findings) {
    const s = inlineSuppression(root, f, cache) || configSuppression(f, config.suppress);
    if (s) { f.status = 'suppressed'; f.suppressedBy = `${s.kind}: ${s.reason}`; }
  }

  // baseline
  const baseline = loadBaseline(root);
  if (useBaseline) applyBaseline(findings, baseline);
  findings.sort(sortFindings);

  // supply-chain diff since the baseline commit
  const deps = [];
  const wantRegistry = mode === 'ci' ? config.supplyChain?.registryMetadata?.ci : config.supplyChain?.registryMetadata?.local;
  for (const lockAbs of detect.lockfiles.npm) {
    try {
      const d = npmLockDiff(root, lockAbs, baseline.commit, { allowedHosts: config.supplyChain?.allowedRegistryHosts });
      if (wantRegistry) {
        for (const p of [...d.added, ...d.changed].slice(0, 40)) {
          const m = await registryMeta(p.name, p.version);
          if (!m) continue;
          p.meta = m;
          if (m.ageDays !== null && m.ageDays < 7) d.flags.push({ kind: 'fresh-publish', package: p.name, version: p.version, detail: `published ${m.ageDays} day(s) ago` });
          if (m.deprecated) d.flags.push({ kind: 'deprecated', package: p.name, version: p.version, detail: 'version is deprecated' });
        }
      }
      deps.push(d);
    } catch (e) { deps.push({ lockfile: lockAbs, error: e.message, added: [], removed: [], changed: [], flags: [] }); }
  }

  const threshold = failOn || config.failOn?.[mode] || 'none';
  const newFindings = findings.filter((f) => f.status === 'new');
  const blocking = newFindings.filter((f) => meetsThreshold(f.severity, threshold)
    || (mode === 'hook' && config.hookFailOnSecrets && f.category === 'secrets'));

  const toolVersions = Object.fromEntries(toolRuns.filter((t) => t.ran).map((t) => [t.tool, t.version]));
  return {
    secscanVersion: SECSCAN_VERSION,
    root, mode, startedAt: new Date(startedAt).toISOString(), finishedAt: nowIso(), ms: Date.now() - startedAt,
    detect: { languages: Object.fromEntries(detect.languages), lockfiles: Object.fromEntries(Object.entries(detect.lockfiles).map(([k, v]) => [k, v.length])), files: detect.fileCount },
    toolRuns, toolVersions,
    threshold,
    counts: summarize(findings),
    findings,
    newFindings,
    blocking,
    resolved: useBaseline ? resolvedSince(findings, baseline) : [],
    baseline: { exists: !!baseline.updatedAt, commit: baseline.commit, updatedAt: baseline.updatedAt, size: Object.keys(baseline.findings).length },
    deps,
    config,
    ok: blocking.length === 0,
  };
}

/**
 * gitleaks' directory mode does not honour .gitignore, so a developer's local .env shows up
 * as if it were in the repo. Those stay visible (a laptop leak still matters) but drop to
 * low, never block, and are labelled so the baseline does not fill with local files.
 */
function markGitignoredSecrets(findings, root, detect) {
  if (!detect.hasGit) return;
  const secrets = findings.filter((f) => f.category === 'secrets' && f.file && !f.extra?.commit);
  if (!secrets.length) return;
  const ignored = gitIgnored(root, [...new Set(secrets.map((f) => f.file))]);
  for (const f of secrets) {
    if (!ignored.has(f.file)) continue;
    f.severity = 'low';
    f.extra.gitignored = true;
    f.message = `[gitignored, local file only] ${f.message}`;
  }
}

function summarize(findings) {
  const c = { total: findings.length, new: 0, baseline: 0, suppressed: 0, bySeverity: {}, byTool: {}, byCategory: {} };
  for (const f of findings) {
    c[f.status] = (c[f.status] || 0) + 1;
    if (f.status === 'new') {
      c.bySeverity[f.severity] = (c.bySeverity[f.severity] || 0) + 1;
      c.byTool[f.tool] = (c.byTool[f.tool] || 0) + 1;
      c.byCategory[f.category] = (c.byCategory[f.category] || 0) + 1;
    }
  }
  return c;
}
