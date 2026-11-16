import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { which, run, sha256File, readJson, exists, IS_WIN } from './util.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const MANIFEST = readJson(path.join(HERE, '..', 'tools.json'));

/** Where downloaded binaries live. SECSCAN_TOOLS_DIR wins; else ~/.secscan/bin. */
export function toolsDir() {
  return process.env.SECSCAN_TOOLS_DIR || path.join(os.homedir(), '.secscan', 'bin');
}

function platformKey() {
  const arch = process.arch === 'x64' ? 'x64' : process.arch === 'arm64' ? 'arm64' : process.arch;
  return `${process.platform}-${arch}`;
}

/**
 * Resolve a tool to an executable path. Order: PATH, tools dir. Never downloads here.
 * Returns { path, source } or { path: null, reason }.
 */
export function resolveTool(name) {
  const dir = toolsDir();
  const onPath = which(name, [dir]);
  if (onPath) return { path: onPath, source: onPath.startsWith(dir) ? 'tools-dir' : 'PATH' };
  const m = MANIFEST[name];
  if (!m) return { path: null, reason: `unknown tool ${name}` };
  if (m.assets) return { path: null, reason: `not installed; run: secscan tools install ${name}` };
  if (m.source?.cargo) return { path: null, reason: `not installed; build with: ${m.source.cargo} (needs Rust) or put ${name} on PATH` };
  if (m.pip) return { path: null, reason: `not installed; pip install ${m.pip} (Linux/macOS/WSL) or put ${name} on PATH` };
  return { path: null, reason: 'not installed' };
}

/** Download + verify + place a pinned binary. Returns the executable path. */
export async function installTool(name, { log = console.error } = {}) {
  const m = MANIFEST[name];
  if (!m) throw new Error(`unknown tool ${name}`);
  if (!m.assets) throw new Error(`${name} has no prebuilt binary; ${resolveTool(name).reason}`);
  const key = platformKey();
  const asset = m.assets[key];
  if (!asset) throw new Error(`${name} ${m.version}: no asset for ${key}`);

  const dir = toolsDir();
  fs.mkdirSync(dir, { recursive: true });
  const target = path.join(dir, asset.bin);
  const stamp = path.join(dir, `.${name}.version`);
  if (exists(target) && exists(stamp) && fs.readFileSync(stamp, 'utf8').trim() === m.version) {
    return target;
  }

  log(`[secscan] downloading ${name} ${m.version} for ${key}`);
  const tmp = path.join(dir, `.${name}.download`);
  const res = await fetch(asset.url, { redirect: 'follow' });
  if (!res.ok) throw new Error(`download failed ${res.status} ${asset.url}`);
  fs.writeFileSync(tmp, Buffer.from(await res.arrayBuffer()));
  const got = sha256File(tmp);
  if (got !== asset.sha256) {
    fs.rmSync(tmp, { force: true });
    throw new Error(`${name}: sha256 mismatch (expected ${asset.sha256}, got ${got}); refusing to install`);
  }

  if (asset.archive) {
    const extractDir = path.join(dir, `.${name}.extract`);
    fs.rmSync(extractDir, { recursive: true, force: true });
    fs.mkdirSync(extractDir, { recursive: true });
    // Windows ships bsdtar (handles zip); Git Bash's GNU tar does not, so pick System32 explicitly.
    const sysTar = IS_WIN ? path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'tar.exe') : null;
    const tar = sysTar && exists(sysTar) ? sysTar : 'tar';
    const r = run(tar, ['-xf', tmp, '-C', extractDir]);
    if (r.status !== 0) throw new Error(`extract failed: ${r.stderr || r.error?.message}`);
    const found = findFile(extractDir, asset.bin);
    if (!found) throw new Error(`${asset.bin} not found in archive`);
    fs.copyFileSync(found, target);
    fs.rmSync(extractDir, { recursive: true, force: true });
    fs.rmSync(tmp, { force: true });
  } else {
    fs.renameSync(tmp, target);
  }
  if (!IS_WIN) fs.chmodSync(target, 0o755);
  fs.writeFileSync(stamp, m.version + '\n');
  log(`[secscan] installed ${target}`);
  return target;
}

function findFile(dir, name) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) { const f = findFile(p, name); if (f) return f; }
    else if (e.name === name) return p;
  }
  return null;
}

export function toolStatus() {
  const out = [];
  for (const name of Object.keys(MANIFEST)) {
    const r = resolveTool(name);
    let version = null;
    if (r.path) version = probeVersion(name, r.path);
    out.push({ name, pinned: MANIFEST[name].version, path: r.path, version, reason: r.reason || null, source: r.source || null });
  }
  return out;
}

function probeVersion(name, bin) {
  const args = name === 'gitleaks' ? ['version'] : ['--version'];
  const r = run(bin, args);
  const text = (r.stdout + r.stderr).trim();
  const m = text.match(/\d+\.\d+(?:\.\d+)?/);
  return m ? m[0] : (text.split('\n')[0] || null);
}
