import * as sighthound from './sighthound.js';
import * as semgrep from './semgrep.js';
import * as npmAudit from './npm-audit.js';
import * as osv from './osv-scanner.js';
import * as gitleaks from './gitleaks.js';
import * as pipAudit from './pip-audit.js';

export const ADAPTERS = [sighthound, semgrep, npmAudit, osv, pipAudit, gitleaks];

/**
 * Decide which adapters run. Returns [{ adapter, enabled, bin, version, reason }].
 * config.adapters[name]: 'auto' | true | false. `only` restricts to a list of names.
 */
export function selectAdapters(config, detect, { only = null, mode = 'local' } = {}) {
  const out = [];
  for (const a of ADAPTERS) {
    const setting = config.adapters?.[a.name] ?? 'auto';
    const row = { adapter: a, name: a.name, category: a.category, enabled: false, bin: null, version: null, reason: null };
    if (only && !only.includes(a.name)) { row.reason = 'not selected'; out.push(row); continue; }
    if (setting === false) { row.reason = 'disabled in config'; out.push(row); continue; }
    if (setting === 'auto' && !a.applies(detect)) { row.reason = 'does not apply to this repo'; out.push(row); continue; }
    if (mode === 'hook' && a.category === 'sast' && a.name === 'semgrep') { row.reason = 'skipped in hook mode'; out.push(row); continue; }
    const av = a.available();
    if (!av.ok) { row.reason = av.reason; out.push(row); continue; }
    row.enabled = true;
    row.bin = av.bin;
    try { row.version = a.version(av.bin); } catch { row.version = null; }
    out.push(row);
  }
  return out;
}
