#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { gitRoot, writeJson, exists, readJson } from '../src/util.js';
import { CONFIG_FILE, STATE_DIR, INVARIANTS_FILE, BASELINE_FILE, DEFAULTS, loadConfig } from '../src/config.js';
import { detectRepo, summarizeDetection } from '../src/detect.js';
import { selectAdapters } from '../src/adapters/index.js';
import { runScan, SECSCAN_VERSION } from '../src/scan.js';
import { toMarkdown, toText } from '../src/report.js';
import { toSarif } from '../src/sarif.js';
import { loadBaseline, saveBaseline, buildBaseline } from '../src/baseline.js';
import { installTool, toolStatus, toolsDir, MANIFEST } from '../src/tools.js';
import { installHook, uninstallHook } from '../src/hook.js';
import { npmLockDiff } from '../src/deps.js';

const HELP = `secscan ${SECSCAN_VERSION} — SAST + secrets + supply chain, one report, fingerprinted baseline.

Usage:
  secscan scan [path] [options]      run a scan (default command)
  secscan init [path] [--hook]       write ${CONFIG_FILE}, ${INVARIANTS_FILE}, scan, write baseline
  secscan baseline update [path]     accept current findings as the baseline
  secscan baseline show [path]       print baseline summary
  secscan deps [path] [--since ref]  lockfile diff (added/changed/removed, install scripts, odd hosts)
  secscan tools [status|install [name…]]
  secscan hook [install|uninstall] [path]
  secscan detect [path]              what the repo looks like and which adapters would run

Scan options:
  --ci                 CI mode: thresholds from failOn.ci (default high), gitleaks history, registry metadata
  --hook               pre-commit mode: staged files only, fast tools, fails on a new secret
  --changed[=<ref>]    restrict SAST/secrets findings to staged files (no ref) or files changed since <ref>
  --only a,b           run only these adapters (sighthound, semgrep, npm-audit, osv-scanner, pip-audit, gitleaks)
  --fail-on <sev>      override threshold: critical|high|medium|low|info|none
  --no-baseline        report everything as new
  --format md|text|json|sarif   stdout format (default: text locally, md in --ci)
  --out <dir>          also write report.md, report.json, results.sarif into <dir>
  --quiet              suppress progress on stderr
  --update-baseline    after the scan, write the baseline (same as baseline update)

Environment:
  SECSCAN_TOOLS_DIR    where downloaded binaries live (default ~/.secscan/bin)
`;

function parseArgs(argv) {
  const args = { _: [], flags: {} };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--') { args._.push(...argv.slice(i + 1)); break; }
    if (a.startsWith('--')) {
      const eq = a.indexOf('=');
      if (eq > 0) args.flags[a.slice(2, eq)] = a.slice(eq + 1);
      else if (['only', 'fail-on', 'format', 'out', 'since', 'changed'].includes(a.slice(2)) && argv[i + 1] && !argv[i + 1].startsWith('--') && a.slice(2) !== 'changed') args.flags[a.slice(2)] = argv[++i];
      else args.flags[a.slice(2)] = true;
    } else args._.push(a);
  }
  return args;
}

/** An explicit path is scanned as given (it may be a subdirectory); otherwise the git root of cwd. */
function resolveRoot(p) {
  if (p) return path.resolve(p);
  return gitRoot(process.cwd()) || process.cwd();
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const cmd = args._[0] && !args._[0].includes(path.sep) && !args._[0].includes('/') && !exists(args._[0]) ? args._.shift() : 'scan';
  const quiet = !!args.flags.quiet;
  const log = quiet ? () => {} : (m) => console.error(m);

  if (args.flags.help || cmd === 'help' || cmd === '--help') { console.log(HELP); return 0; }
  if (args.flags.version || cmd === 'version') { console.log(SECSCAN_VERSION); return 0; }

  switch (cmd) {
    case 'scan': return cmdScan(args, log);
    case 'init': return cmdInit(args, log);
    case 'baseline': return cmdBaseline(args, log);
    case 'deps': return cmdDeps(args);
    case 'tools': return cmdTools(args, log);
    case 'hook': return cmdHook(args);
    case 'detect': return cmdDetect(args);
    default: console.error(`unknown command: ${cmd}\n`); console.log(HELP); return 2;
  }
}

async function cmdScan(args, log) {
  const root = resolveRoot(args._[0]);
  const mode = args.flags.hook ? 'hook' : args.flags.ci ? 'ci' : 'local';
  let changed = null;
  if (mode === 'hook') changed = 'staged';
  else if (args.flags.changed === true) changed = 'staged';
  else if (typeof args.flags.changed === 'string') changed = args.flags.changed;
  const only = args.flags.only ? String(args.flags.only).split(',').map((s) => s.trim()) : null;
  const r = await runScan(root, {
    mode, only, changed, useBaseline: !args.flags['no-baseline'], failOn: args.flags['fail-on'] || null, log,
  });
  const format = args.flags.format || (mode === 'ci' ? 'md' : 'text');
  emit(r, format);
  if (args.flags.out) writeOutputs(r, path.resolve(args.flags.out), log);
  if (args.flags['update-baseline']) {
    const prev = loadBaseline(root);
    saveBaseline(root, buildBaseline(root, r.findings, r.toolVersions, prev));
    log(`[secscan] baseline written: ${path.join(root, BASELINE_FILE)}`);
  }
  return r.ok ? 0 : 1;
}

function emit(r, format) {
  if (format === 'json') console.log(JSON.stringify(stripConfig(r), null, 2));
  else if (format === 'sarif') console.log(JSON.stringify(toSarif(r.findings, { toolVersions: r.toolVersions, secscanVersion: r.secscanVersion }), null, 2));
  else if (format === 'md') console.log(toMarkdown(r));
  else console.log(toText(r));
}

function stripConfig(r) { const { config, ...rest } = r; return rest; }

function writeOutputs(r, dir, log) {
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'report.md'), toMarkdown(r) + '\n');
  writeJson(path.join(dir, 'report.json'), stripConfig(r));
  writeJson(path.join(dir, 'results.sarif'), toSarif(r.findings, { toolVersions: r.toolVersions, secscanVersion: r.secscanVersion }));
  log(`[secscan] wrote report.md, report.json, results.sarif to ${dir}`);
}

async function cmdInit(args, log) {
  const root = resolveRoot(args._[0]);
  const detect = detectRepo(root);
  log(`[secscan] ${root}\n[secscan] ${summarizeDetection(detect)}`);
  const cfgPath = path.join(root, CONFIG_FILE);
  if (!exists(cfgPath)) {
    const cfg = { version: 1, adapters: { ...DEFAULTS.adapters }, exclude: DEFAULTS.exclude, failOn: DEFAULTS.failOn, suppress: [], semgrep: { configs: suggestedSemgrep(detect) } };
    writeJson(cfgPath, cfg);
    log(`[secscan] wrote ${CONFIG_FILE}`);
  } else log(`[secscan] ${CONFIG_FILE} exists, kept`);
  const invPath = path.join(root, INVARIANTS_FILE);
  if (!exists(invPath)) {
    fs.mkdirSync(path.dirname(invPath), { recursive: true });
    fs.writeFileSync(invPath, INVARIANTS_TEMPLATE);
    log(`[secscan] wrote ${INVARIANTS_FILE} (edit it: these are the rules Claude checks the diff against)`);
  }
  for (const row of selectAdapters(loadConfig(root), detect)) {
    log(`[secscan] ${row.enabled ? 'will run ' : 'skipping '} ${row.name.padEnd(12)} ${row.enabled ? row.version || '' : row.reason}`);
  }
  const r = await runScan(root, { mode: 'local', useBaseline: false, log });
  console.log(toText(r));
  saveBaseline(root, buildBaseline(root, r.findings, r.toolVersions, null));
  log(`[secscan] baseline written with ${r.findings.filter((f) => f.status !== 'suppressed').length} finding(s): ${BASELINE_FILE}`);
  log('[secscan] review them with: secscan scan --no-baseline --format md');
  if (args.flags.hook) { const h = installHook(root); log(`[secscan] hook ${h.action}: ${h.file}`); }
  log(`[secscan] commit ${CONFIG_FILE} and ${STATE_DIR}/ to share the baseline with CI.`);
  return 0;
}

function suggestedSemgrep(detect) {
  const c = ['p/default', 'p/secrets'];
  if (detect.languages.has('javascript') || detect.languages.has('typescript')) c.push('p/javascript', 'p/nodejs', 'p/react', 'p/typescript');
  if (detect.languages.has('python')) c.push('p/python', 'p/django', 'p/flask');
  if (detect.languages.has('go')) c.push('p/golang');
  if (detect.languages.has('java')) c.push('p/java');
  if (detect.languages.has('ruby')) c.push('p/ruby');
  if (detect.languages.has('php')) c.push('p/php');
  if (detect.languages.has('csharp')) c.push('p/csharp');
  c.push('p/owasp-top-ten');
  return [...new Set(c)];
}

async function cmdBaseline(args, log) {
  const sub = args._[0] === 'update' || args._[0] === 'show' ? args._.shift() : 'show';
  const root = resolveRoot(args._[0]);
  if (sub === 'update') {
    const r = await runScan(root, { mode: 'local', useBaseline: false, log });
    const prev = loadBaseline(root);
    const b = buildBaseline(root, r.findings, r.toolVersions, prev);
    saveBaseline(root, b);
    console.log(`baseline updated: ${Object.keys(b.findings).length} finding(s) at ${b.commit?.slice(0, 8) || 'no commit'}`);
    return 0;
  }
  const b = loadBaseline(root);
  if (!b.updatedAt) { console.log('no baseline'); return 0; }
  const by = {};
  for (const f of Object.values(b.findings)) by[`${f.tool} ${f.severity}`] = (by[`${f.tool} ${f.severity}`] || 0) + 1;
  console.log(`baseline: ${Object.keys(b.findings).length} finding(s), updated ${b.updatedAt}, commit ${b.commit || '-'}`);
  for (const [k, n] of Object.entries(by).sort()) console.log(`  ${n.toString().padStart(4)}  ${k}`);
  return 0;
}

function cmdDeps(args) {
  const root = resolveRoot(args._[0]);
  const detect = detectRepo(root);
  const since = args.flags.since || loadBaseline(root).commit || null;
  const cfg = loadConfig(root);
  if (!detect.lockfiles.npm.length) { console.log('no package-lock.json found'); return 0; }
  for (const lock of detect.lockfiles.npm) {
    const d = npmLockDiff(root, lock, since, { allowedHosts: cfg.supplyChain?.allowedRegistryHosts });
    console.log(`${d.lockfile}: ${d.total} packages, since ${since ? since.slice(0, 8) : '(none: everything counts as added)'}: +${d.added.length} ~${d.changed.length} -${d.removed.length}`);
    for (const fl of d.flags) console.log(`  ⚠ ${fl.kind.padEnd(14)} ${fl.package}@${fl.version}  ${fl.detail}`);
    for (const p of d.added.slice(0, 50)) console.log(`  + ${p.name}@${p.version}${p.dev ? ' (dev)' : ''}${p.hasInstallScript ? '  [install script]' : ''}`);
    for (const p of d.changed.slice(0, 50)) console.log(`  ~ ${p.name} ${p.from} → ${p.version}`);
    for (const p of d.removed.slice(0, 20)) console.log(`  - ${p.name}@${p.version}`);
  }
  return 0;
}

async function cmdTools(args, log) {
  const sub = args._[0] === 'install' || args._[0] === 'status' ? args._.shift() : 'status';
  if (sub === 'install') {
    const names = args._.length ? args._ : Object.keys(MANIFEST).filter((n) => MANIFEST[n].assets);
    let failed = 0;
    for (const n of names) {
      try { const p = await installTool(n, { log }); console.log(`${n}: ${p}`); }
      catch (e) { failed++; console.error(`${n}: ${e.message}`); }
    }
    return failed ? 1 : 0;
  }
  console.log(`tools dir: ${toolsDir()}`);
  for (const t of toolStatus()) {
    console.log(`  ${t.name.padEnd(12)} pinned ${t.pinned.padEnd(8)} ${t.path ? `${t.version || '?'} at ${t.path}` : `missing — ${t.reason}`}`);
  }
  return 0;
}

function cmdHook(args) {
  const sub = args._[0] === 'install' || args._[0] === 'uninstall' ? args._.shift() : 'install';
  const root = resolveRoot(args._[0]);
  const r = sub === 'install' ? installHook(root) : uninstallHook(root);
  console.log(`${r.action}: ${r.file}`);
  return 0;
}

function cmdDetect(args) {
  const root = resolveRoot(args._[0]);
  const detect = detectRepo(root);
  console.log(`${root}\n${summarizeDetection(detect)}`);
  for (const row of selectAdapters(loadConfig(root), detect)) {
    console.log(`  ${row.enabled ? '✓' : '·'} ${row.name.padEnd(12)} ${row.enabled ? `${row.version || ''} ${row.bin}` : row.reason}`);
  }
  return 0;
}

const INVARIANTS_TEMPLATE = `# Security invariants

Plain-language rules that must hold in this repo. The secscan triager checks each diff
against them. Scanners cannot see these; only a reader of the code can.

<!-- Replace the examples with the rules for this codebase. Keep each one testable by reading code. -->

- Every query on a table that has a tenant/workspace/org column filters by the caller's tenant.
- Every inbound webhook validates the provider's signature before reading the body.
- No user- or tenant-supplied text is interpolated into an LLM system prompt.
- Outbound HTTP requests to user-supplied URLs go through the egress allow-list.
- Secrets are read from the environment or a secrets manager, never from source or config files.
`;

main().then((code) => process.exit(code)).catch((e) => { console.error(`secscan: ${e.stack || e.message}`); process.exit(2); });
