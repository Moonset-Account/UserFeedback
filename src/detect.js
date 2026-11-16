import path from 'node:path';
import { walk, exists } from './util.js';

const SOURCE_EXT = {
  javascript: ['.js', '.jsx', '.mjs', '.cjs'],
  typescript: ['.ts', '.tsx', '.mts', '.cts'],
  python: ['.py'],
  java: ['.java'],
  go: ['.go'],
  ruby: ['.rb'],
  php: ['.php'],
  csharp: ['.cs'],
  html: ['.html', '.htm'],
};

/**
 * Detect what a repo contains, so adapters set to "auto" can decide whether to run.
 * Returns { languages: Set, lockfiles: { npm: [...abs paths], ... }, hasGit, fileCount }.
 */
export function detectRepo(root) {
  const languages = new Map();
  const lockfiles = { npm: [], pnpm: [], yarn: [], python: [], pythonRequirements: [], go: [], cargo: [], gemfile: [], composer: [] };
  const manifests = { pyproject: [] };
  let fileCount = 0;

  for (const file of walk(root)) {
    fileCount++;
    const base = path.basename(file);
    const ext = path.extname(file).toLowerCase();
    for (const [lang, exts] of Object.entries(SOURCE_EXT)) {
      if (exts.includes(ext)) languages.set(lang, (languages.get(lang) || 0) + 1);
    }
    if (base === 'package-lock.json' || base === 'npm-shrinkwrap.json') lockfiles.npm.push(file);
    else if (base === 'pnpm-lock.yaml') lockfiles.pnpm.push(file);
    else if (base === 'yarn.lock') lockfiles.yarn.push(file);
    else if (/^requirements[\w.-]*\.txt$/.test(base)) { lockfiles.python.push(file); lockfiles.pythonRequirements.push(file); }
    else if (base === 'poetry.lock' || base === 'Pipfile.lock' || base === 'uv.lock' || base === 'pdm.lock') lockfiles.python.push(file);
    else if (base === 'pyproject.toml') manifests.pyproject.push(file);
    else if (base === 'go.mod') lockfiles.go.push(file);
    else if (base === 'Cargo.lock') lockfiles.cargo.push(file);
    else if (base === 'Gemfile.lock') lockfiles.gemfile.push(file);
    else if (base === 'composer.lock') lockfiles.composer.push(file);
  }

  const anyLockfile = Object.values(lockfiles).some((a) => a.length > 0);
  return {
    languages,
    lockfiles,
    manifests,
    anyLockfile,
    hasGit: exists(path.join(root, '.git')),
    fileCount,
  };
}

export function summarizeDetection(d) {
  const langs = [...d.languages.entries()].sort((a, b) => b[1] - a[1]).map(([l, n]) => `${l} (${n})`).join(', ') || 'none';
  const locks = Object.entries(d.lockfiles).filter(([k, v]) => v.length && k !== 'pythonRequirements').map(([k, v]) => `${k} x${v.length}`).join(', ') || 'none';
  const gaps = d.manifests?.pyproject?.length && !d.lockfiles.python.length ? '; pyproject.toml without a lockfile (python supply chain not covered: add uv.lock, poetry.lock or a pinned requirements.txt)' : '';
  return `languages: ${langs}; lockfiles: ${locks}; files: ${d.fileCount}${gaps}`;
}
