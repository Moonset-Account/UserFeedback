// 验收用例 1：可接受结果路径 + 重复处置幂等 + 输入校验。
import { test, describe, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import { QuarantineManager, InvalidDetectionError } from '../src/quarantine.js';
import { makeHarness, sha256Text } from './helpers.mjs';

describe('隔离与还原的可接受结果 / 幂等', () => {
  let harness;
  beforeEach(async () => {
    harness = await makeHarness();
  });
  afterEach(async () => {
    await harness.cleanup();
  });

  test('正常隔离：原位置文件移入唯一副本，记录保留对象标识、摘要、原属主', async () => {
    const content = 'malicious-sample-A';
    const samplePath = await harness.writeSample('a.bin', content);
    const detection = harness.detection({ objectId: 'det-1', samplePath, content });

    const manager = await new QuarantineManager(harness.storeDir).init();
    const result = await manager.quarantine(detection);

    assert.equal(result.status, 'quarantined');
    const entry = manager.getStatus('det-1');
    assert.equal(entry.status, 'quarantined');
    assert.equal(entry.owner, 'host-1/user-a');
    assert.equal(entry.digest, sha256Text(content));
    assert.equal(entry.originalPath, path.resolve(samplePath));

    // 原位置已腾空，隔离区只有一个有效副本且内容一致。
    const copies = await readdir(path.join(harness.storeDir, 'copies'));
    assert.deepEqual(copies, ['det-1.bin']);
    assert.equal(await readFile(entry.copyPath, 'utf8'), content);
  });

  test('误报还原：副本放回原位置，内容与摘要完全一致，副本随即清除', async () => {
    const content = 'false-positive-sample';
    const samplePath = await harness.writeSample('fp.bin', content);
    const detection = harness.detection({ objectId: 'det-2', samplePath, content });

    const manager = await new QuarantineManager(harness.storeDir).init();
    await manager.quarantine(detection);
    const restoreResult = await manager.restore('det-2');

    assert.equal(restoreResult.status, 'restored');
    assert.equal(restoreResult.conflict, false);
    assert.equal(await readFile(samplePath, 'utf8'), content);
    assert.equal(sha256Text(await readFile(samplePath, 'utf8')), detection.digest);
    assert.equal(manager.getStatus('det-2').status, 'restored');
    const copies = await readdir(path.join(harness.storeDir, 'copies'));
    assert.deepEqual(copies, []);
  });

  test('重复执行同一隔离处置只留下一个有效副本', async () => {
    const content = 'repeat-sample';
    const samplePath = await harness.writeSample('r.bin', content);
    const detection = harness.detection({ objectId: 'det-3', samplePath, content });

    const manager = await new QuarantineManager(harness.storeDir).init();
    await manager.quarantine(detection);
    const second = await manager.quarantine(detection);
    const third = await manager.quarantine(detection);

    assert.equal(second.status, 'already-quarantined');
    assert.equal(third.status, 'already-quarantined');
    const copies = await readdir(path.join(harness.storeDir, 'copies'));
    assert.deepEqual(copies, ['det-3.bin']);
  });

  test('重复还原幂等：不报错、不在原位置产生新文件', async () => {
    const content = 'restore-once';
    const samplePath = await harness.writeSample('ro.bin', content);
    const detection = harness.detection({ objectId: 'det-4', samplePath, content });

    const manager = await new QuarantineManager(harness.storeDir).init();
    await manager.quarantine(detection);
    await manager.restore('det-4');
    const again = await manager.restore('det-4');

    assert.equal(again.status, 'already-restored');
    assert.equal(await readFile(samplePath, 'utf8'), content);
  });

  test('检测结果缺字段或摘要格式非法时拒绝处置（阻断结果）', async () => {
    const manager = await new QuarantineManager(harness.storeDir).init();
    const content = 'x';
    const samplePath = await harness.writeSample('bad.bin', content);
    const base = harness.detection({ objectId: 'det-5', samplePath, content });

    await assert.rejects(
      () => manager.quarantine({ ...base, digest: 'not-a-sha256' }),
      (err) => err instanceof InvalidDetectionError && err.code === 'INVALID_DETECTION',
    );
    await assert.rejects(
      () => manager.quarantine({ ...base, owner: '   ' }),
      (err) => err.code === 'INVALID_DETECTION',
    );
    await assert.rejects(
      () => manager.quarantine({ ...base, objectId: '' }),
      (err) => err.code === 'INVALID_DETECTION',
    );
    // 被拒绝后清单中不残留半成品条目。
    assert.equal(manager.getStatus('det-5'), null);
  });
});
