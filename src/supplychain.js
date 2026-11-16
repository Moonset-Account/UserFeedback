import fs from 'node:fs';
import path from 'node:path';

/**
 * Merge supply-chain findings that describe the same advisory on the same package from
 * different tools (npm audit + osv-scanner report the same GHSA). Keeps one finding,
 * prefers the one with an exact version and fixedIn, records the other tool in extra.
 * Also marks dev-only packages using the lockfile.
 */
export function dedupeSupplyChain(findings, root) {
  const devIndex = new Map(); // lockfileRel -> Map(name -> dev boolean, by name@version and by name)
  const groups = new Map();
  const out = [];

  for (const f of findings) {
    if (f.category !== 'supply-chain') { out.push(f); continue; }
    const adv = advisoryId(f);
    const key = adv ? `${f.file}|${String(f.extra?.package || '').toLowerCase()}|${adv}` : null;
    if (!key) { out.push(f); continue; }
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(f);
  }

  for (const list of groups.values()) {
    list.sort((a, b) => score(b) - score(a));
    const keep = list[0];
    const others = list.slice(1);
    if (others.length) {
      const otherTools = [...new Set(others.map((o) => o.tool).filter((t) => t !== keep.tool))];
      if (otherTools.length) keep.extra.alsoReportedBy = otherTools;
      // the same tool can emit one advisory under several ids (PYSEC + GHSA); keep the ids as aliases
      const ids = new Set([...(keep.extra.aliases || [])]);
      for (const o of others) { ids.add(o.ruleId.split('/').pop()); for (const a of o.extra?.aliases || []) ids.add(a); }
      ids.delete(keep.ruleId.split('/').pop());
      keep.extra.aliases = [...ids];
      // carry npm audit's fix hint if osv had none
      for (const o of others) {
        // osv sometimes has no summary for PYSEC ids; borrow the other tool's title
        if (!keep.extra.title && o.extra?.title) {
          keep.extra.title = o.extra.title;
          const bareId = keep.ruleId.split('/').pop();
          keep.message = keep.message.replace(`: ${bareId}.`, `: ${o.extra.title}.`).replace(`: ${bareId}`, `: ${o.extra.title}`);
        }
        if (!keep.extra.fixedIn && o.extra?.fixAvailable) keep.extra.fixAvailable = o.extra.fixAvailable;
        if (keep.extra.direct === undefined && o.extra?.direct !== undefined) keep.extra.direct = o.extra.direct;
      }
      if (keep.extra.direct !== undefined && !/direct dependency|transitive/.test(keep.message)) {
        keep.message += keep.extra.direct ? ' (direct dependency)' : ' (transitive)';
      }
    }
    out.push(keep);
  }

  for (const f of out) {
    if (f.category !== 'supply-chain' || !f.file || !f.extra?.package) continue;
    const dev = isDev(root, f.file, f.extra.package, f.extra.version, devIndex);
    if (dev !== null) {
      f.extra.dev = dev;
      if (dev && !/\(dev\)/.test(f.message)) f.message += ' (dev)';
    }
  }
  return out;
}

export function advisoryId(f) {
  const fromRule = f.ruleId.match(/GHSA-[\w-]+|CVE-\d+-\d+/i)?.[0];
  if (fromRule) return fromRule.toUpperCase();
  for (const a of f.extra?.aliases || []) {
    const m = String(a).match(/GHSA-[\w-]+/i);
    if (m) return m[0].toUpperCase();
  }
  for (const a of f.extra?.aliases || []) {
    const m = String(a).match(/CVE-\d+-\d+/i);
    if (m) return m[0].toUpperCase();
  }
  return null;
}

function score(f) {
  let s = 0;
  if (f.extra?.fixedIn) s += 2;
  if (f.extra?.version && !/[<>=~^ ]/.test(f.extra.version)) s += 2; // exact version beats a range
  if (f.tool === 'osv-scanner') s += 1;
  return s;
}

function isDev(root, lockRel, name, version, index) {
  if (!index.has(lockRel)) {
    const m = new Map();
    try {
      const json = JSON.parse(fs.readFileSync(path.join(root, lockRel), 'utf8'));
      for (const [key, p] of Object.entries(json.packages || {})) {
        if (!key) continue;
        const n = p.name || key.replace(/^.*node_modules\//, '');
        const dev = !!(p.dev || p.devOptional);
        m.set(`${n}@${p.version}`, dev);
        // a package is "dev" for the repo only if every copy is dev
        m.set(n, m.has(n) ? m.get(n) && dev : dev);
      }
    } catch { /* leave empty */ }
    index.set(lockRel, m);
  }
  const m = index.get(lockRel);
  if (!m.size) return null;
  if (version && m.has(`${name}@${version}`)) return m.get(`${name}@${version}`);
  return m.has(name) ? m.get(name) : null;
}
