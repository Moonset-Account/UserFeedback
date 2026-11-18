// 验收用例 2：内容变化暂停重新确认、原位置缺失、隔离中断重启续判（交错场景）。
import { test, describe, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { writeFile, readFile, readdir, rm } from 'node:fs/promises';
import path from 'node:path';
import {
  QuarantineManager,
  ReconfirmRequiredError,
  MissingOriginalError,
  CopyTamperedError,
} from '../src/quarantine.js';
import { makeHarness, sha256Text } from './helpers.mjs';

describe('重新确认阻断与重启续判', () => {
  let harness;
  beforeEach(async () => {
    harness = await makeHarness();
  });
  afterEach(async () => {
    await harness.cleanup();
  });

  test('扫描后内容已变化：隔离暂停并要求重新确认，原文件与副本都不被改动', async () => {
    const content = 'original-content';
    const samplePath = await harness.writeSample('chg.bin', content);
    const detection = harness.detection({ objectId: 'det-10', samplePath, content });

    const manager = await new QuarantineManager(harness.storeDir).init();
    await writeFile(samplePath, 'content-changed-after-scan', 'utf8');

    await assert.rejects(
      () => manager.quarantine(detection),
      (err) => {
        assert.ok(err instanceof ReconfirmRequiredError);
        assert.equal(err.code, 'RECONFIRM_REQUIRED');
        assert.equal(err.details.currentDigest, sha256Text('content-changed-after-scan'));
        assert.equal(err.details.expectedDigest, detection.digest);
        return true;
      },
    );

    // 阻断时原文件保留原样，隔离区没有产出任何副本。
    assert.equal(await readFile(samplePath, 'utf8'), 'content-changed-after-scan');
    assert.deepEqual(await readdir(path.join(harness.storeDir, 'copies')), []);
    assert.equal(manager.getStatus('det-10').status, 'quarantining');
  });

  test('携带与当前磁盘一致的新摘要重新确认后，隔离继续完成（阻断→放行交错）', async () => {
    const content = 'original-content';
    const samplePath = await harness.writeSample('chg2.bin', content);
    const stale = harness.detection({ objectId: 'det-11', samplePath, content });

    const manager = await new QuarantineManager(harness.storeDir).init();
    const newContent = 'reconfirmed-content';
    await writeFile(samplePath, newContent, 'utf8');
    await assert.rejects(() => manager.quarantine(stale), (err) => err.code === 'RECONFIRM_REQUIRED');

    // 模拟检测系统重新扫描后下发新摘要。
    const fresh = harness.detection({ objectId: 'det-11', samplePath, content: newContent });
    const result = await manager.quarantine(fresh);
    assert.equal(result.status, 'quarantined');
    assert.equal(result.entry.digest, sha256Text(newContent));
    assert.equal(await readFile(result.entry.copyPath, 'utf8'), newContent);
    assert.equal(result.entry.reconfirmCount, 1);
  });

  test('首次处置时原位置已缺失：拒绝隔离而不是凭空造副本', async () => {
    const content = 'ghost';
    const samplePath = harness.samplePath('ghost.bin');
    const detection = harness.detection({ objectId: 'det-12', samplePath, content });

    const manager = await new QuarantineManager(harness.storeDir).init();
    await assert.rejects(
      () => manager.quarantine(detection),
      (err) => err instanceof MissingOriginalError && err.code === 'MISSING_ORIGINAL',
    );
    assert.deepEqual(await readdir(path.join(harness.storeDir, 'copies')), []);
  });

  test('中断在“副本已落盘、原文件未删除”阶段：重启后续判删除原文件并收尾', async () => {
    const content = 'interrupted-before-delete';
    const samplePath = await harness.writeSample('int1.bin', content);
    const detection = harness.detection({ objectId: 'det-13', samplePath, content });

    // 构造中断现场：先完成一次隔离（副本有效、原文件已删），再把样本写回原位置
    // 并把清单状态拨回 quarantining，等价于“副本已复制、原文件还没删进程就死了”。
    let manager = await new QuarantineManager(harness.storeDir).init();
    await manager.quarantine(detection);
    const fs = await import('node:fs/promises');
    await writeFile(samplePath, content, 'utf8');
    const manifestPath = path.join(harness.storeDir, 'manifest.json');
    const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
    manifest.entries['det-13'].status = 'quarantining';
    await fs.writeFile(manifestPath, JSON.stringify(manifest), 'utf8');

    // 全新进程实例（无任何内存状态）执行 reconcile。
    manager = await new QuarantineManager(harness.storeDir).init();
    const report = await manager.reconcile();
    const item = report.find((r) => r.objectId === 'det-13');
    assert.equal(item.status, 'quarantined');
    assert.equal(item.action, 'resumed');
    assert.equal(manager.getStatus('det-13').status, 'quarantined');
    await assert.rejects(() => readFile(samplePath), (err) => err.code === 'ENOENT');
    assert.deepEqual(await readdir(path.join(harness.storeDir, 'copies')), ['det-13.bin']);
  });

  test('中断在“原文件已删除、副本待校验”阶段：重启后凭副本摘要收尾', async () => {
    const content = 'interrupted-after-delete';
    const samplePath = await harness.writeSample('int2.bin', content);
    const detection = harness.detection({ objectId: 'det-14', samplePath, content });

    let manager = await new QuarantineManager(harness.storeDir).init();
    await manager.quarantine(detection);
    const entry = manager.getStatus('det-14');

    // 模拟进程在最终状态落盘前死亡：原文件已不存在，只把状态拨回进行中。
    const fs = await import('node:fs/promises');
    const manifestPath = path.join(harness.storeDir, 'manifest.json');
    const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
    manifest.entries['det-14'].status = 'quarantining';
    await fs.writeFile(manifestPath, JSON.stringify(manifest), 'utf8');
    assert.equal(await readFile(entry.copyPath, 'utf8'), content);

    manager = await new QuarantineManager(harness.storeDir).init();
    const report = await manager.reconcile();
    assert.equal(report.find((r) => r.objectId === 'det-14').status, 'quarantined');
    assert.equal(manager.getStatus('det-14').status, 'quarantined');
  });

  test('中断副本被篡改：续判报告阻断且拒绝把损坏副本当作有效隔离', async () => {
    const content = 'tampered-copy';
    const samplePath = await harness.writeSample('int3.bin', content);
    const detection = harness.detection({ objectId: 'det-15', samplePath, content });

    let manager = await new QuarantineManager(harness.storeDir).init();
    await manager.quarantine(detection);

    const fs = await import('node:fs/promises');
    const manifestPath = path.join(harness.storeDir, 'manifest.json');
    const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
    const copyPath = manifest.entries['det-15'].copyPath;
    // 原文件保持缺失，仅副本内容被改坏，且状态回到进行中。
    await writeFile(copyPath, 'corrupted-bytes', 'utf8');
    manifest.entries['det-15'].status = 'quarantining';
    await fs.writeFile(manifestPath, JSON.stringify(manifest), 'utf8');

    manager = await new QuarantineManager(harness.storeDir).init();
    const report = await manager.reconcile();
    const item = report.find((r) => r.objectId === 'det-15');
    assert.equal(item.status, 'blocked');
    assert.equal(item.reason, 'COPY_TAMPERED');
    assert.ok(item.message.length > 0);

    // 再次制造篡改现场后直接跑隔离，损坏副本同样不会被提升为有效副本。
    await writeFile(copyPath, 'corrupted-bytes-again', 'utf8');
    await assert.rejects(() => manager.quarantine(detection), (err) => err instanceof CopyTamperedError);
    await rm(copyPath, { force: true });
  });
});
