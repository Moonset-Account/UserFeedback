import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { run, exists, IS_WIN } from './util.js';

const MARK = '# secscan pre-commit hook';
const BIN = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'bin', 'secscan.js');

function hooksDir(root) {
  const r = run('git', ['config', '--get', 'core.hooksPath'], { cwd: root });
  const custom = r.status === 0 ? r.stdout.trim() : '';
  if (custom) return path.isAbsolute(custom) ? custom : path.join(root, custom);
  const g = run('git', ['rev-parse', '--git-path', 'hooks'], { cwd: root });
  const p = g.status === 0 ? g.stdout.trim() : '.git/hooks';
  return path.isAbsolute(p) ? p : path.join(root, p);
}

export function installHook(root) {
  const dir = hooksDir(root);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, 'pre-commit');
  let chain = '';
  if (exists(file)) {
    const cur = fs.readFileSync(file, 'utf8');
    if (cur.includes(MARK)) return { file, action: 'already installed' };
    const backup = file + '.pre-secscan';
    fs.copyFileSync(file, backup);
    chain = `\n# previous hook, kept by secscan\nif [ -x "${toPosix(backup)}" ]; then "${toPosix(backup)}" "$@" || exit $?; fi\n`;
  }
  const bin = toPosix(BIN);
  const script = `#!/bin/sh
${MARK}
# Fast mode on staged files: Sighthound + gitleaks + lockfile diff. Blocks only on a new secret
# (or on failOn.hook from .secscan.json). Bypass once with: git commit --no-verify
${chain}
if command -v node >/dev/null 2>&1; then
  node "${bin}" scan --hook || exit $?
elif command -v secscan >/dev/null 2>&1; then
  secscan scan --hook || exit $?
else
  echo "secscan: node not found, skipping pre-commit scan" >&2
fi
`;
  fs.writeFileSync(file, script, { mode: 0o755 });
  if (!IS_WIN) fs.chmodSync(file, 0o755);
  return { file, action: chain ? 'installed (previous hook chained)' : 'installed' };
}

export function uninstallHook(root) {
  const file = path.join(hooksDir(root), 'pre-commit');
  if (!exists(file) || !fs.readFileSync(file, 'utf8').includes(MARK)) return { file, action: 'not installed' };
  const backup = file + '.pre-secscan';
  if (exists(backup)) { fs.copyFileSync(backup, file); fs.rmSync(backup); return { file, action: 'restored previous hook' }; }
  fs.rmSync(file);
  return { file, action: 'removed' };
}

function toPosix(p) { return p.split(path.sep).join('/'); }
