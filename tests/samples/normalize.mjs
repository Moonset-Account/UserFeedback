// Rewrites the saved scanner samples so their paths are relative POSIX, independent of the
// machine that produced them. Run after refreshing a sample: node tests/samples/normalize.mjs
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const strip = (p) => String(p).replace(/\\/g, '/').replace(/^.*\/fixtures\/node-vuln\//, '').replace(/^\.\//, '');

const sh = JSON.parse(fs.readFileSync(path.join(HERE, 'sighthound.json'), 'utf8'));
for (const f of sh) f.file = strip(f.file);
fs.writeFileSync(path.join(HERE, 'sighthound.json'), JSON.stringify(sh, null, 2) + '\n');

const osv = JSON.parse(fs.readFileSync(path.join(HERE, 'osv-scanner.json'), 'utf8'));
for (const r of osv.results) r.source.path = strip(r.source.path);
fs.writeFileSync(path.join(HERE, 'osv-scanner.json'), JSON.stringify(osv, null, 2) + '\n');

const gl = JSON.parse(fs.readFileSync(path.join(HERE, 'gitleaks.json'), 'utf8'));
for (const l of gl) l.File = strip(l.File);
fs.writeFileSync(path.join(HERE, 'gitleaks.json'), JSON.stringify(gl, null, 2) + '\n');

console.log('sighthound:', [...new Set(sh.map((f) => f.file))].join(', '));
console.log('osv:', osv.results.map((r) => r.source.path).join(', '));
console.log('gitleaks:', gl.map((l) => l.File).join(', '));
