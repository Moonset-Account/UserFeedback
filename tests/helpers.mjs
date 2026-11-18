// 测试夹具：在系统临时目录中创建独立的样本目录与处置工作区，
// 所有夹具均为本地合成数据，不触碰真实端点。
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';

export function sha256Text(text) {
  return createHash('sha256').update(text, 'utf8').digest('hex');
}

export async function makeHarness(prefix = 'qtest') {
  const base = await mkdtemp(path.join(os.tmpdir(), `${prefix}-`));
  const sampleDir = path.join(base, 'samples');
  const storeDir = path.join(base, 'quarantine-store');
  await mkdir(sampleDir, { recursive: true });
  await mkdir(storeDir, { recursive: true });

  return {
    base,
    sampleDir,
    storeDir,
    samplePath(name) {
      return path.join(sampleDir, name);
    },
    async writeSample(name, content) {
      const target = path.join(sampleDir, name);
      await writeFile(target, content, 'utf8');
      return target;
    },
    detection({ objectId, samplePath, content, owner = 'host-1/user-a', summary = 'unit-test hit' }) {
      return {
        objectId,
        originalPath: samplePath,
        digest: sha256Text(content),
        owner,
        summary,
      };
    },
    async cleanup() {
      await rm(base, { recursive: true, force: true });
    },
  };
}
