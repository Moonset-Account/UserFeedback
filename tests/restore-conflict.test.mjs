// 验收用例 3：还原冲突双份保留、同内容占用边界、多对象交错、CLI 真实入口。
import { test, describe, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { writeFile, readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { QuarantineManager, RestoreConflictError } from '../src/quarantine.js';
import { makeHarness, sha256Text } from './helpers.mjs';

const execFileAsync = promisify(execFile);
const CLI = path.resolve('src/cli.js');

describe('还原冲突与边界交错', () => {
  let harness;
  beforeEach(async () => {
    harness = await makeHarness();
  });
  afterEach(async () => {
    await harness.cleanup();
  });

  test('原位置被不同内容对象占用：两份内容都保留、写冲突标记、原位置回到隔离对象', async () => {
    const quarantinedContent = 'quarantined-object';
    const occupantContent = 'another-object-took-the-slot';
    const samplePath = await harness.writeSample('slot.bin', quarantinedContent);
    const detection = harness.detection({ objectId: 'det-20', samplePath, content: quarantinedContent });

    const manager = await new QuarantineManager(harness.storeDir).init();
    await manager.quarantine(detection);

    // 隔离期间原位置被别的对象占用。
    await writeFile(samplePath, occupantContent, 'utf8');

    await assert.rejects(
      () => manager.restore('det-20'),
      (err) => {
        assert.ok(err instanceof RestoreConflictError);
        assert.equal(err.code, 'RESTORE_CONFLICT');
        assert.equal(err.details.occupantDigest, sha256Text(occupantContent));
        assert.equal(err.details.quarantinedDigest, sha256Text(quarantinedContent));
        return true;
      },
    );

    // 原位置已回到隔离对象内容。
    assert.equal(await readFile(samplePath, 'utf8'), quarantinedContent);

    // 冲突目录保留两份内容且不互相覆盖，并带 sidecar 标记。
    const conflictFiles = (await readdir(`${harness.storeDir}/conflicts`)).sort();
    assert.equal(conflictFiles.length, 3); // 占用者、还原对象、.conflict.json 标记
    const marker = conflictFiles.find((name) => name.endsWith('.conflict.json'));
    const markerData = JSON.parse(
      await readFile(path.join(harness.storeDir, 'conflicts', marker), 'utf8'),
    );
    assert.equal(markerData.objectId, 'det-20');
    assert.equal(markerData.occupantDigest, sha256Text(occupantContent));
    const occupantKept = conflictFiles.find((name) => name === 'slot.bin');
    const restoredKept = conflictFiles.find((name) => name.startsWith('restored-slot'));
    assert.ok(occupantKept, '占用者内容必须保留');
    assert.ok(restoredKept, '隔离对象内容必须保留');
    assert.equal(
      await readFile(path.join(harness.storeDir, 'conflicts', occupantKept), 'utf8'),
      occupantContent,
    );
    assert.equal(
      await readFile(path.join(harness.storeDir, 'conflicts', restoredKept), 'utf8'),
      quarantinedContent,
    );

    // 处置已闭环：状态 restored 且记录冲突信息，隔离副本已清除。
    const entry = manager.getStatus('det-20');
    assert.equal(entry.status, 'restored');
    assert.ok(entry.conflict);
    assert.deepEqual(await readdir(path.join(harness.storeDir, 'copies')), []);
  });

  test('边界：占用者与隔离对象摘要一致时不产生冲突副本，直接还原', async () => {
    const content = 'identical-bytes';
    const samplePath = await harness.writeSample('same.bin', content);
    const detection = harness.detection({ objectId: 'det-21', samplePath, content });

    const manager = await new QuarantineManager(harness.storeDir).init();
    await manager.quarantine(detection);
    await writeFile(samplePath, content, 'utf8'); // 同内容占用

    const result = await manager.restore('det-21');
    assert.equal(result.conflict, false);
    assert.equal(result.sameContent, true);
    assert.deepEqual(await readdir(path.join(harness.storeDir, 'conflicts')), []);
    assert.equal(await readFile(samplePath, 'utf8'), content);
  });

  test('多对象交错处置：各自只持有一个副本，一个阻断不影响另一个完成', async () => {
    const contentA = 'sample-A';
    const contentB = 'sample-B';
    const pathA = await harness.writeSample('a.bin', contentA);
    const pathB = await harness.writeSample('b.bin', contentB);
    const detA = harness.detection({ objectId: 'det-22', samplePath: pathA, content: contentA });
    const detB = harness.detection({ objectId: 'det-23', samplePath: pathB, content: contentB });

    const manager = await new QuarantineManager(harness.storeDir).init();
    await manager.quarantine(detA);
    // B 在处置前被改动，必须被阻断。
    await writeFile(pathB, 'mutated-B', 'utf8');
    await assert.rejects(() => manager.quarantine(detB), (err) => err.code === 'RECONFIRM_REQUIRED');

    // A 的隔离与还原不受 B 的阻断影响。
    assert.equal(manager.getStatus('det-22').status, 'quarantined');
    assert.equal(manager.getStatus('det-23').status, 'quarantining');
    await manager.restore('det-22');
    assert.equal(await readFile(pathA, 'utf8'), contentA);

    // B 重新确认后正常完成，且两对象副本互不串扰。
    const freshB = harness.detection({ objectId: 'det-23', samplePath: pathB, content: 'mutated-B' });
    await manager.quarantine(freshB);
    const copies = await readdir(path.join(harness.storeDir, 'copies'));
    assert.deepEqual(copies, ['det-23.bin']); // A 的副本已随还原清除
  });

  test('CLI 真实入口：隔离→状态查询→重启 reconcile→还原 全链路可执行', async () => {
    const content = 'cli-e2e-sample';
    const samplePath = await harness.writeSample('cli.bin', content);
    const detectionPath = path.join(harness.base, 'detection.json');
    await writeFile(
      detectionPath,
      JSON.stringify(
        harness.detection({ objectId: 'det-24', samplePath, content, owner: 'host-cli/user-1' }),
      ),
      'utf8',
    );

    const run = (args) => execFileAsync(process.execPath, [CLI, '--root', harness.storeDir, ...args]);

    const q = await run(['quarantine', '--detection', detectionPath]);
    assert.match(q.stdout, /"status": "quarantined"/);

    const status = await run(['status', '--object-id', 'det-24']);
    assert.match(status.stdout, /"objectId": "det-24"/);
    assert.match(status.stdout, /"owner": "host-cli\/user-1"/);

    // 模拟重启：新进程对同一工作区执行 reconcile，应识别为已隔离而跳过。
    const reconcile = await run(['reconcile']);
    assert.match(reconcile.stdout, /"action": "skipped"/);

    const restore = await run(['restore', '--object-id', 'det-24']);
    assert.match(restore.stdout, /"status": "restored"/);
    assert.equal(await readFile(samplePath, 'utf8'), content);
  });

  test('CLI 真实入口：内容变化时以退出码 10 拒绝隔离', async () => {
    const content = 'cli-block-sample';
    const samplePath = await harness.writeSample('block.bin', content);
    const detectionPath = path.join(harness.base, 'detection-block.json');
    await writeFile(
      detectionPath,
      JSON.stringify(harness.detection({ objectId: 'det-25', samplePath, content })),
      'utf8',
    );
    await writeFile(samplePath, 'changed-before-quarantine', 'utf8');

    await assert.rejects(
      execFileAsync(process.execPath, [CLI, '--root', harness.storeDir, 'quarantine', '--detection', detectionPath]),
      (err) => {
        assert.equal(err.code, 10);
        assert.match(err.stderr, /RECONFIRM_REQUIRED/);
        return true;
      },
    );
  });

  test('CLI 真实入口：还原冲突时以退出码 11 报告且双份已落盘', async () => {
    const content = 'cli-conflict-sample';
    const samplePath = await harness.writeSample('cf.bin', content);
    const detectionPath = path.join(harness.base, 'detection-cf.json');
    await writeFile(
      detectionPath,
      JSON.stringify(harness.detection({ objectId: 'det-26', samplePath, content })),
      'utf8',
    );
    const run = (args) => execFileAsync(process.execPath, [CLI, '--root', harness.storeDir, ...args]);
    await run(['quarantine', '--detection', detectionPath]);
    await writeFile(samplePath, 'occupant-claims-path', 'utf8');

    await assert.rejects(run(['restore', '--object-id', 'det-26']), (err) => {
      assert.equal(err.code, 11);
      assert.match(err.stderr, /RESTORE_CONFLICT/);
      return true;
    });
    const conflictFiles = await readdir(path.join(harness.storeDir, 'conflicts'));
    assert.equal(conflictFiles.length, 3);
    assert.equal(await readFile(samplePath, 'utf8'), content);
  });
});
