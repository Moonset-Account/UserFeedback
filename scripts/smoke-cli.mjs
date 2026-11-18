// 手工冒烟脚本：用本地合成样本驱动真实 CLI 子进程，验证退出码与落盘布局。
// 运行：node scripts/smoke-cli.mjs
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash } from 'node:crypto';

const execFileAsync = promisify(execFile);
const sha256 = (text) => createHash('sha256').update(text, 'utf8').digest('hex');
const cli = path.resolve('src/cli.js');
const root = await mkdtemp(path.join(os.tmpdir(), 'q-smoke-'));

try {
  const sampleDir = path.join(root, 'samples');
  const storeDir = path.join(root, 'store');
  await mkdir(sampleDir, { recursive: true });
  await mkdir(storeDir, { recursive: true });

  const samplePath = path.join(sampleDir, 's.bin');
  await writeFile(samplePath, 'smoke-malware-bytes', 'utf8');
  const detPath = path.join(root, 'det.json');
  await writeFile(
    detPath,
    JSON.stringify({
      objectId: 'smoke-1',
      originalPath: samplePath,
      digest: sha256('smoke-malware-bytes'),
      owner: 'host-x/user-y',
      summary: 'smoke hit',
    }),
    'utf8',
  );

  const run = (args) => execFileAsync(process.execPath, [cli, '--root', storeDir, ...args]);

  const q = await run(['quarantine', '--detection', detPath]);
  console.log('quarantine ->', JSON.parse(q.stdout).status);

  const rec = await run(['reconcile']);
  console.log('reconcile  ->', JSON.parse(rec.stdout)[0].action);

  await writeFile(samplePath, 'occupant-new-bytes', 'utf8');
  let conflictCode = 0;
  try {
    await run(['restore', '--object-id', 'smoke-1']);
  } catch (error) {
    conflictCode = error.code;
    console.log('restore    -> blocked code', error.code, JSON.parse(error.stderr).code);
  }
  if (conflictCode !== 11) throw new Error('冲突还原退出码应为 11');
  console.log('conflicts/ ->', (await readdir(path.join(storeDir, 'conflicts'))).sort().join(', '));
  console.log('original   ->', await readFile(samplePath, 'utf8'));

  // 内容变化阻断：退出码 10。
  const s2 = path.join(sampleDir, 's2.bin');
  await writeFile(s2, 'changed-on-disk', 'utf8');
  const det2 = path.join(root, 'det2.json');
  await writeFile(
    det2,
    JSON.stringify({ objectId: 'smoke-2', originalPath: s2, digest: sha256('changed-on-disk'), owner: 'host-x/user-z' }),
    'utf8',
  );
  await writeFile(s2, 'tampered-after-scan', 'utf8');
  let blockCode = 0;
  try {
    await run(['quarantine', '--detection', det2]);
  } catch (error) {
    blockCode = error.code;
    console.log('reconfirm  -> blocked code', error.code, JSON.parse(error.stderr).code);
  }
  if (blockCode !== 10) throw new Error('重新确认阻断退出码应为 10');

  console.log('SMOKE OK');
} finally {
  await rm(root, { recursive: true, force: true });
}
