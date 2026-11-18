// 文件系统与摘要工具：全部使用 node 内置模块，无外部依赖。
import { createHash } from 'node:crypto';
import {
  mkdir,
  readFile,
  writeFile,
  rename,
  copyFile,
  access,
  readdir,
} from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import path from 'node:path';

const HEX_64 = /^[0-9a-f]{64}$/i;

export function isSha256Hex(value) {
  return typeof value === 'string' && HEX_64.test(value);
}

// 流式计算文件 SHA-256，避免把整份样本读进内存。
export function sha256File(filePath) {
  return new Promise((resolve, reject) => {
    const hash = createHash('sha256');
    const stream = createReadStream(filePath);
    stream.on('error', reject);
    stream.on('data', (chunk) => hash.update(chunk));
    stream.on('end', () => resolve(hash.digest('hex')));
  });
}

export async function pathExists(targetPath) {
  try {
    await access(targetPath);
    return true;
  } catch {
    return false;
  }
}

export async function ensureDir(dirPath) {
  await mkdir(dirPath, { recursive: true });
}

// 先写同目录临时文件再 rename，保证清单文件要么是旧版本、要么是新版本，
// 不会在进程被杀死时留下半截 JSON。
export async function atomicWriteJson(targetPath, value) {
  await ensureDir(path.dirname(targetPath));
  const tmpPath = `${targetPath}.tmp-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  await writeFile(tmpPath, JSON.stringify(value, null, 2), 'utf8');
  await rename(tmpPath, targetPath);
}

// 同样以 temp + rename 的方式落隔离副本 / 还原文件，避免半成品被当成有效副本。
export async function atomicCopy(srcPath, destPath) {
  await ensureDir(path.dirname(destPath));
  const tmpPath = `${destPath}.tmp-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  await copyFile(srcPath, tmpPath);
  await rename(tmpPath, destPath);
}

export async function readJsonIfExists(filePath) {
  if (!(await pathExists(filePath))) return null;
  return JSON.parse(await readFile(filePath, 'utf8'));
}

// 冲突目录中生成不覆盖既有内容的文件名：name、name-1、name-2……
export async function uniquePath(dirPath, baseName) {
  await ensureDir(dirPath);
  const ext = path.extname(baseName);
  const stem = path.basename(baseName, ext);
  let candidate = path.join(dirPath, baseName);
  let counter = 1;
  while (await pathExists(candidate)) {
    candidate = path.join(dirPath, `${stem}-${counter}${ext}`);
    counter += 1;
  }
  return candidate;
}

export { readdir };
