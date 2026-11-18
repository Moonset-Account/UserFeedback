// 端点响应处置模块：对检测系统确认的可疑对象执行隔离，并为误报保留还原能力。
//
// 设计约束（对应题面）：
//  1. 消费检测系统给出的对象标识 objectId、摘要 digest（SHA-256）、原属主 owner，
//     以及原位置 originalPath、可选摘要说明 summary；使用样本文件模拟隔离。
//  2. 扫描后文件内容若已变化（当前磁盘摘要 != 检测摘要），暂停隔离并抛出
//     RECONFIRM_REQUIRED，要求检测系统携带新摘要重新确认。
//  3. 还原时原位置已被别的对象占用：内容不同则两份内容都保留到 conflicts/ 并落
//     冲突标记（RESTORE_CONFLICT）；内容一致则不重复保留。
//  4. 隔离中断重启后，依据清单 manifest.json 与隔离副本继续判断对象位置和摘要，
//     不依赖进程内状态。
//  5. 同一处置重复执行只保留一个有效隔离副本（copies/<objectId>.bin），
//     重复还原不产生新文件。
import path from 'node:path';
import { rm } from 'node:fs/promises';
import {
  QuarantineError,
  InvalidDetectionError,
  ReconfirmRequiredError,
  MissingOriginalError,
  MissingCopyError,
  CopyTamperedError,
  RestoreConflictError,
} from './errors.js';
import {
  isSha256Hex,
  sha256File,
  pathExists,
  ensureDir,
  atomicWriteJson,
  atomicCopy,
  readJsonIfExists,
  uniquePath,
} from './storage.js';

const MANIFEST_VERSION = 1;
const STATUS = Object.freeze({
  QUARANTINING: 'quarantining', // 隔离进行中（含中断残留）
  QUARANTINED: 'quarantined', // 已隔离，副本有效
  RESTORED: 'restored', // 已还原（可能带冲突标记）
});

export class QuarantineManager {
  // rootDir 为处置工作区：copies/ 存放隔离副本，conflicts/ 存放冲突双份，
  // manifest.json 持久化全部处置状态。
  constructor(rootDir, options = {}) {
    this.root = path.resolve(rootDir);
    this.copyDir = path.join(this.root, 'copies');
    this.conflictDir = path.join(this.root, 'conflicts');
    this.manifestPath = path.join(this.root, 'manifest.json');
    this.maxHistory = options.maxHistory ?? 500;
    this.manifest = null;
  }

  async init() {
    await ensureDir(this.root);
    await ensureDir(this.copyDir);
    await ensureDir(this.conflictDir);
    const data = await readJsonIfExists(this.manifestPath);
    if (data && data.version === MANIFEST_VERSION) {
      this.manifest = data;
      this.manifest.entries ??= {};
      this.manifest.events ??= [];
    } else {
      this.manifest = { version: MANIFEST_VERSION, createdAt: new Date().toISOString(), entries: {}, events: [] };
      await this.#persist();
    }
    return this;
  }

  // ---- 公共 API -------------------------------------------------------------

  // 执行（或续跑）一次隔离处置。detection 来自检测系统：
  // { objectId, originalPath, digest(sha256 hex), owner, summary? }
  async quarantine(rawDetection) {
    this.#ensureReady();
    const detection = this.validateDetection(rawDetection);
    const { objectId } = detection;
    let entry = this.manifest.entries[objectId];

    if (!entry) {
      entry = this.#newEntry(detection);
      this.manifest.entries[objectId] = entry;
      this.#record('detected', objectId, { originalPath: entry.originalPath, digest: entry.digest });
      await this.#persist();
    } else {
      // 同一对象身份的原位置不允许漂移，防止把 A 路径的处置套到 B 路径上。
      if (entry.originalPath !== detection.originalPath) {
        throw new InvalidDetectionError(
          '同一对象标识关联了不同的原位置，拒绝处置',
          { objectId, recordedPath: entry.originalPath, suppliedPath: detection.originalPath },
        );
      }
      entry.owner = detection.owner;
      entry.summary = detection.summary;
    }

    // 已隔离：重复执行同一处置，只校验唯一副本，绝不产生第二个副本。
    if (entry.status === STATUS.QUARANTINED) {
      await this.#verifyCopy(entry);
      this.#record('quarantine-repeated', objectId, {
        originalRepopulated: await pathExists(entry.originalPath),
      });
      await this.#persist();
      return { status: 'already-quarantined', resumed: false, entry: this.#publicEntry(entry) };
    }

    // 已还原对象再次被检测：复用同一条记录重新进入隔离流程。
    if (entry.status === STATUS.RESTORED) {
      entry.status = STATUS.QUARANTINING;
      entry.digest = detection.digest;
      entry.conflict = null;
      entry.restoredAt = null;
      entry.updatedAt = new Date().toISOString();
      this.#record('requarantine', objectId, { digest: entry.digest });
      await this.#persist();
    }

    return this.#capture(entry, detection);
  }

  // 中断重启后的续判入口：扫描清单中所有 quarantining 记录，逐条根据
  // “原位置文件 / 隔离副本”的实际位置与摘要决定收尾、续拷或阻断。
  async reconcile() {
    this.#ensureReady();
    const report = [];
    for (const entry of Object.values(this.manifest.entries)) {
      if (entry.status !== STATUS.QUARANTINING) {
        report.push({ objectId: entry.objectId, status: entry.status, action: 'skipped' });
        continue;
      }
      try {
        const result = await this.#capture(entry, { digest: entry.digest });
        report.push({
          objectId: entry.objectId,
          status: STATUS.QUARANTINED,
          action: result.resumed ? 'resumed' : 'completed',
        });
      } catch (error) {
        report.push({
          objectId: entry.objectId,
          status: 'blocked',
          reason: error.code ?? 'UNKNOWN_ERROR',
          message: error.message,
          details: error.details ?? {},
        });
      }
    }
    return report;
  }

  // 误报还原：把隔离副本放回原位置。原位置被占用且内容不同时，
  // 两份内容都保留到 conflicts/ 并写冲突标记，同时抛出 RESTORE_CONFLICT。
  async restore(objectId) {
    this.#ensureReady();
    const entry = this.manifest.entries[objectId];
    if (!entry) {
      throw new QuarantineError('UNKNOWN_OBJECT', '清单中不存在该处置对象', { objectId });
    }
    if (entry.status === STATUS.RESTORED) {
      this.#record('restore-repeated', objectId, {});
      await this.#persist();
      return { status: 'already-restored', conflict: false, entry: this.#publicEntry(entry) };
    }
    if (entry.status !== STATUS.QUARANTINED) {
      throw new QuarantineError(
        'NOT_QUARANTINED',
        '对象尚未完成隔离，无法还原（请先完成隔离或重新确认）',
        { objectId, status: entry.status },
      );
    }

    await this.#verifyCopy(entry);
    await ensureDir(path.dirname(entry.originalPath));

    // 原位置空闲：直接原子放回。
    if (!(await pathExists(entry.originalPath))) {
      await atomicCopy(entry.copyPath, entry.originalPath);
      await this.#finishRestore(entry, 'restored', null);
      return { status: 'restored', conflict: false, entry: this.#publicEntry(entry) };
    }

    const occupantDigest = await sha256File(entry.originalPath);
    if (occupantDigest === entry.digest) {
      // 占用者与隔离对象内容完全一致：没有保留两份的必要，直接收尾。
      await this.#finishRestore(entry, 'restore-same-content', null);
      return {
        status: 'restored',
        conflict: false,
        sameContent: true,
        entry: this.#publicEntry(entry),
      };
    }

    // 冲突：占用者、隔离对象各保留一份，并写 sidecar 标记冲突来源。
    const baseName = path.basename(entry.originalPath);
    const occupantConflictPath = await uniquePath(this.conflictDir, baseName);
    await atomicCopy(entry.originalPath, occupantConflictPath);
    const restoredConflictPath = await uniquePath(this.conflictDir, `restored-${baseName}`);
    await atomicCopy(entry.copyPath, restoredConflictPath);
    const markerPath = `${restoredConflictPath}.conflict.json`;
    const conflictInfo = {
      at: new Date().toISOString(),
      originalPath: entry.originalPath,
      occupantDigest,
      occupantPath: occupantConflictPath,
      quarantinedDigest: entry.digest,
      restoredCopyPath: restoredConflictPath,
      markerPath,
    };
    await atomicWriteJson(markerPath, { objectId: entry.objectId, owner: entry.owner, ...conflictInfo });

    // 原位置放回隔离对象内容，处置流程闭环。
    await atomicCopy(entry.copyPath, entry.originalPath);
    await this.#finishRestore(entry, 'restore-conflict', conflictInfo);
    throw new RestoreConflictError(
      '原位置已被其他对象占用且内容不同，两份内容均已保留并标记冲突',
      { objectId: entry.objectId, ...conflictInfo },
    );
  }

  getStatus(objectId) {
    this.#ensureReady();
    const entry = this.manifest.entries[objectId];
    return entry ? this.#publicEntry(entry) : null;
  }

  listEntries() {
    this.#ensureReady();
    return Object.values(this.manifest.entries).map((entry) => this.#publicEntry(entry));
  }

  getEvents() {
    this.#ensureReady();
    return [...this.manifest.events];
  }

  // 校验并规范化检测系统输入。
  validateDetection(detection) {
    if (!detection || typeof detection !== 'object') {
      throw new InvalidDetectionError('检测结果必须是对象', { received: typeof detection });
    }
    const { objectId, originalPath, digest, owner } = detection;
    if (typeof objectId !== 'string' || !objectId.trim()) {
      throw new InvalidDetectionError('objectId 必须是非空字符串', { field: 'objectId' });
    }
    if (typeof originalPath !== 'string' || !originalPath.trim()) {
      throw new InvalidDetectionError('originalPath 必须是非空字符串', { field: 'originalPath' });
    }
    if (!isSha256Hex(digest)) {
      throw new InvalidDetectionError('digest 必须是 64 位十六进制 SHA-256 摘要', { field: 'digest' });
    }
    if (typeof owner !== 'string' || !owner.trim()) {
      throw new InvalidDetectionError('owner 必须是非空字符串', { field: 'owner' });
    }
    if (detection.summary !== undefined && typeof detection.summary !== 'string') {
      throw new InvalidDetectionError('summary 若提供必须是字符串', { field: 'summary' });
    }
    return {
      objectId: objectId.trim(),
      originalPath: path.resolve(originalPath),
      digest: digest.toLowerCase(),
      owner: owner.trim(),
      summary: detection.summary ?? '',
    };
  }

  // ---- 内部实现 -------------------------------------------------------------

  // 隔离核心：根据原位置文件与副本的当前状态推进，可被新检测调用也可被
  // reconcile 在重启后调用（此时 detection 只带清单里记录的摘要）。
  async #capture(entry, detection) {
    const originalExists = await pathExists(entry.originalPath);

    // 场景 A：原位置已经空了——说明删除原文件这一步可能已完成，靠副本收尾。
    if (!originalExists) {
      if (await pathExists(entry.copyPath)) {
        const copyDigest = await sha256File(entry.copyPath);
        if (copyDigest === entry.digest) {
          await this.#finalizeQuarantine(entry, 'resumed-after-restart');
          return { status: STATUS.QUARANTINED, resumed: true, entry: this.#publicEntry(entry) };
        }
        await rm(entry.copyPath, { force: true });
        throw new CopyTamperedError(
          '隔离副本摘要与处置记录不一致，副本已隔离失效，需人工介入',
          { objectId: entry.objectId, expectedDigest: entry.digest, actualDigest: copyDigest },
        );
      }
      throw new MissingOriginalError(
        '原位置文件已不存在且没有任何隔离副本，无法继续处置',
        { objectId: entry.objectId, originalPath: entry.originalPath },
      );
    }

    // 场景 B：原位置文件仍在。若副本已在且有效，说明这是“拷贝完成、
    // 删除原文件前进程死亡”的中断现场，重启续判应标记 resumed。
    let resumed = false;
    if (await pathExists(entry.copyPath)) {
      const existingDigest = await sha256File(entry.copyPath);
      if (existingDigest === entry.digest) resumed = true;
    }

    // 先比对磁盘摘要，内容变化必须暂停重新确认。
    const currentDigest = await sha256File(entry.originalPath);
    if (currentDigest !== entry.digest) {
      if (detection.digest === currentDigest) {
        // 检测系统携带与当前磁盘一致的新摘要重新下发：视为重新确认完成。
        entry.digest = currentDigest;
        entry.reconfirmCount += 1;
        entry.updatedAt = new Date().toISOString();
        this.#record('reconfirmed', entry.objectId, { digest: currentDigest });
      } else {
        this.#record('quarantine-blocked', entry.objectId, {
          reason: 'content-changed',
          expectedDigest: entry.digest,
          currentDigest,
        });
        await this.#persist();
        throw new ReconfirmRequiredError(
          '扫描后文件内容已变化，处置暂停，请由检测系统重新确认后再隔离',
          {
            objectId: entry.objectId,
            originalPath: entry.originalPath,
            expectedDigest: entry.digest,
            currentDigest,
          },
        );
      }
    }

    // 场景 C：存在中断残留副本，摘要不符就清掉重拷，保证只留一个有效副本。
    if (await pathExists(entry.copyPath)) {
      const copyDigest = await sha256File(entry.copyPath);
      if (copyDigest !== entry.digest) {
        await rm(entry.copyPath, { force: true });
        this.#record('stale-copy-removed', entry.objectId, { staleDigest: copyDigest });
      }
    }
    if (!(await pathExists(entry.copyPath))) {
      await atomicCopy(entry.originalPath, entry.copyPath);
    }
    await this.#verifyCopy(entry);

    await rm(entry.originalPath, { force: true });
    await this.#finalizeQuarantine(entry, resumed ? 'resumed-after-restart' : 'quarantined');
    return { status: STATUS.QUARANTINED, resumed, entry: this.#publicEntry(entry) };
  }

  async #verifyCopy(entry) {
    if (!(await pathExists(entry.copyPath))) {
      throw new MissingCopyError('隔离副本缺失，无法继续该处置', {
        objectId: entry.objectId,
        copyPath: entry.copyPath,
      });
    }
    const actualDigest = await sha256File(entry.copyPath);
    if (actualDigest !== entry.digest) {
      throw new CopyTamperedError('隔离副本摘要与记录不一致，疑似损坏或被篡改', {
        objectId: entry.objectId,
        expectedDigest: entry.digest,
        actualDigest,
      });
    }
  }

  async #finalizeQuarantine(entry, action) {
    entry.status = STATUS.QUARANTINED;
    entry.updatedAt = new Date().toISOString();
    this.#record(action, entry.objectId, {
      copyPath: entry.copyPath,
      digest: entry.digest,
    });
    await this.#persist();
  }

  async #finishRestore(entry, action, conflictInfo) {
    const now = new Date().toISOString();
    entry.status = STATUS.RESTORED;
    entry.restoredAt = now;
    entry.updatedAt = now;
    entry.conflict = conflictInfo;
    this.#record(action, entry.objectId, conflictInfo ?? {});
    await rm(entry.copyPath, { force: true });
    await this.#persist();
  }

  #newEntry(detection) {
    const now = new Date().toISOString();
    return {
      objectId: detection.objectId,
      originalPath: detection.originalPath,
      owner: detection.owner,
      summary: detection.summary,
      digest: detection.digest,
      status: STATUS.QUARANTINING,
      copyPath: this.#copyPathFor(detection.objectId),
      conflict: null,
      reconfirmCount: 0,
      createdAt: now,
      updatedAt: now,
      restoredAt: null,
    };
  }

  #copyPathFor(objectId) {
    const safeId = objectId.replace(/[^a-zA-Z0-9._-]/g, '_');
    return path.join(this.copyDir, `${safeId}.bin`);
  }

  #record(action, objectId, details) {
    this.manifest.events.push({
      at: new Date().toISOString(),
      objectId,
      action,
      details: details ?? {},
    });
    if (this.manifest.events.length > this.maxHistory) {
      this.manifest.events.splice(0, this.manifest.events.length - this.maxHistory);
    }
  }

  #publicEntry(entry) {
    return { ...entry };
  }

  #ensureReady() {
    if (!this.manifest) {
      throw new QuarantineError('NOT_INITIALIZED', 'QuarantineManager 使用前必须先 await init()');
    }
  }

  async #persist() {
    await atomicWriteJson(this.manifestPath, this.manifest);
  }
}

export { STATUS, MANIFEST_VERSION };
export {
  QuarantineError,
  InvalidDetectionError,
  ReconfirmRequiredError,
  MissingOriginalError,
  MissingCopyError,
  CopyTamperedError,
  RestoreConflictError,
};
