import path from 'node:path';
import { readJson, exists } from './util.js';

export const CONFIG_FILE = '.secscan.json';
export const STATE_DIR = '.secscan';
export const BASELINE_FILE = path.join(STATE_DIR, 'baseline.json');
export const INVARIANTS_FILE = path.join(STATE_DIR, 'invariants.md');

export const DEFAULTS = {
  version: 1,
  // "auto" enables an adapter when the repo looks like it applies; true/false forces it.
  adapters: { sighthound: 'auto', semgrep: 'auto', 'npm-audit': 'auto', 'osv-scanner': 'auto', 'pip-audit': 'auto', gitleaks: 'auto' },
  exclude: ['**/node_modules/**', '**/dist/**', '**/build/**', '**/coverage/**', '**/*.min.js', '**/vendor/**'],
  // Minimum severity of a NEW finding that fails the run, per mode.
  failOn: { local: 'none', ci: 'high', hook: 'none' },
  // The commit hook always fails on a new secret, whatever failOn.hook says.
  hookFailOnSecrets: true,
  // Suppressions: [{ tool?, rule?, path?, reason, expires? (YYYY-MM-DD) }]
  suppress: [],
  semgrep: { configs: ['p/default'], extraArgs: [] },
  gitleaks: { history: { local: false, ci: true } },
  supplyChain: { registryMetadata: { local: false, ci: true }, allowedRegistryHosts: ['registry.npmjs.org'] },
};

export function loadConfig(root) {
  const file = path.join(root, CONFIG_FILE);
  const user = exists(file) ? readJson(file) : {};
  return deepMerge(structuredClone(DEFAULTS), user);
}

export function deepMerge(base, over) {
  if (Array.isArray(over)) return over.slice();
  if (over && typeof over === 'object') {
    const out = { ...(base && typeof base === 'object' && !Array.isArray(base) ? base : {}) };
    for (const [k, v] of Object.entries(over)) out[k] = deepMerge(out[k], v);
    return out;
  }
  return over === undefined ? base : over;
}
