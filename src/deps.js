import fs from 'node:fs';
import path from 'node:path';
import { gitShow, relPosix } from './util.js';

/**
 * Lockfile diff for npm (lockfileVersion 2/3): what changed since a git ref.
 * Returns { lockfile, since, added: [...], removed: [...], changed: [...], flags: [...] } per lockfile.
 * Each package row: { name, version, resolved, hasInstallScript, dev, host }.
 */
export function npmLockDiff(root, lockAbs, sinceRef, { allowedHosts = ['registry.npmjs.org'] } = {}) {
  const rel = relPosix(root, lockAbs);
  const nowText = fs.readFileSync(lockAbs, 'utf8');
  const oldText = sinceRef ? gitShow(root, sinceRef, rel) : null;
  const now = parseLock(nowText);
  const old = oldText ? parseLock(oldText) : new Map();

  const added = [], removed = [], changed = [];
  for (const [name, p] of now) {
    const o = old.get(name);
    if (!o) added.push(p);
    else if (o.version !== p.version || o.resolved !== p.resolved) changed.push({ ...p, from: o.version });
  }
  for (const [name, p] of old) if (!now.has(name)) removed.push(p);

  const flags = [];
  for (const p of [...added, ...changed]) {
    if (p.hasInstallScript) flags.push({ kind: 'install-script', package: p.name, version: p.version, detail: 'runs a preinstall/install/postinstall script' });
    if (p.host && !allowedHosts.includes(p.host)) flags.push({ kind: 'registry-host', package: p.name, version: p.version, detail: `resolved from ${p.host}` });
    if (p.resolved && /^git\+|^git:|github:|^https?:\/\/github\.com\//.test(p.resolved)) flags.push({ kind: 'git-dependency', package: p.name, version: p.version, detail: p.resolved });
    if (p.resolved && /^http:\/\//.test(p.resolved)) flags.push({ kind: 'insecure-url', package: p.name, version: p.version, detail: p.resolved });
  }
  return { lockfile: rel, since: sinceRef, added, removed, changed, flags, total: now.size };
}

function parseLock(text) {
  const out = new Map();
  let json;
  try { json = JSON.parse(text); } catch { return out; }
  if (json.packages) {
    for (const [key, p] of Object.entries(json.packages)) {
      if (!key) continue; // root
      const name = p.name || key.replace(/^.*node_modules\//, '');
      // one entry per name@version to keep the diff readable; nested duplicates collapse
      const id = `${name}@${p.version}`;
      if (out.has(id)) continue;
      out.set(id, row(name, p));
    }
  } else if (json.dependencies) {
    walkV1(json.dependencies, out);
  }
  return out;
}

function walkV1(deps, out) {
  for (const [name, p] of Object.entries(deps || {})) {
    const id = `${name}@${p.version}`;
    if (!out.has(id)) out.set(id, row(name, p));
    if (p.dependencies) walkV1(p.dependencies, out);
  }
}

function row(name, p) {
  let host = null;
  try { if (p.resolved && /^https?:/.test(p.resolved)) host = new URL(p.resolved).host; } catch { /* ignore */ }
  return {
    name, version: p.version || null, resolved: p.resolved || null,
    hasInstallScript: !!p.hasInstallScript, dev: !!p.dev, host,
  };
}

/** Optional registry lookup: publish date and maintainer count for a version. Network. */
export async function registryMeta(name, version, { timeoutMs = 4000 } = {}) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(`https://registry.npmjs.org/${encodeURIComponent(name).replace('%40', '@')}`, { signal: ctl.signal, headers: { accept: 'application/json' } });
    if (!r.ok) return null;
    const j = await r.json();
    const publishedAt = j.time?.[version] || null;
    const ageDays = publishedAt ? Math.floor((Date.now() - new Date(publishedAt).getTime()) / 86400000) : null;
    return { publishedAt, ageDays, maintainers: (j.maintainers || []).length, deprecated: !!j.versions?.[version]?.deprecated };
  } catch { return null; } finally { clearTimeout(t); }
}

export function lockfilesUnder(root, detect) {
  return detect.lockfiles.npm.map((abs) => ({ abs, rel: relPosix(root, abs) }));
}

export { path };
