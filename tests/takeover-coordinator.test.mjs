import test from 'node:test';
import assert from 'node:assert/strict';

import { TakeoverCoordinator, TakeoverError } from '../src/takeover-coordinator.mjs';

test('可接受结果：完整接管生命周期，影子预览不提交，迟到旧回执只补历史', () => {
  const c = new TakeoverCoordinator();
  c.registerSource('sensor-1', { firstSeq: 1 });

  // 旧版负责期：1..3 提交给旧版
  for (const seq of [1, 2, 3]) {
    const r = c.ingest('sensor-1', seq, { kind: 'proc', seq });
    assert.equal(r.committedTo, 'old');
    assert.deepEqual(r.previewedBy, []);
  }
  c.reportOldWatermark('sensor-1', 3);
  c.reportOldBackfillLimit('sensor-1', 3);

  // 新版准备确认：进入影子期，4..5 双收但仍由旧版提交
  c.confirmNewReady('sensor-1');
  for (const seq of [4, 5]) {
    const r = c.ingest('sensor-1', seq);
    assert.equal(r.committedTo, 'old');
    assert.deepEqual(r.previewedBy, ['new']);
  }
  c.reportOldWatermark('sensor-1', 5);
  c.reportOldBackfillLimit('sensor-1', 5);

  // 接管：边界 = 水位 + 1，6 起由新版提交
  const snap = c.commitTakeover('sensor-1', 6);
  assert.equal(snap.owner, 'new');
  assert.equal(snap.takeoverBoundary, 6);
  assert.equal(c.ingest('sensor-1', 6).committedTo, 'new');
  assert.equal(c.ingest('sensor-1', 7).committedTo, 'new');

  // 新版提交 6 的处置；旧版提交自己拥有的 4
  assert.equal(c.submitDisposition('sensor-1', 6, { by: 'new', decision: 'blocked' }).applied, true);
  assert.equal(c.submitDisposition('sensor-1', 4, { by: 'old', decision: 'allowed' }).applied, true);

  // 迟到的旧版回执针对 6：只进历史，不改变新版已提交的决定
  const late = c.submitDisposition('sensor-1', 6, { by: 'old', decision: 'allowed' });
  assert.equal(late.applied, false);
  const after = c.snapshot('sensor-1');
  const commit6 = after.commits.find((x) => x.seq === 6);
  assert.equal(commit6.disposition, 'blocked');
  assert.equal(commit6.dispositionBy, 'new');
  assert.ok(after.history.some((h) => h.seq === 6 && h.decision === 'allowed'));

  // 任一时刻每个序号都有负责方
  assert.equal(c.verifyCoverage('sensor-1').ok, true);
});

test('应被阻止：未就绪接管、越水位边界、新版越权提交、重复序号', () => {
  const c = new TakeoverCoordinator();
  c.registerSource('sensor-2', { firstSeq: 1 });
  c.ingest('sensor-2', 1);
  c.ingest('sensor-2', 2);
  c.reportOldWatermark('sensor-2', 2);

  // 新版未确认准备，接管被拒绝
  assert.throws(() => c.commitTakeover('sensor-2', 3), (e) => {
    assert.ok(e instanceof TakeoverError);
    assert.equal(e.code, 'NEW_NOT_READY');
    return true;
  });

  c.confirmNewReady('sensor-2');
  c.ingest('sensor-2', 3);
  c.ingest('sensor-2', 4);

  // 边界越过旧版消费水位 + 1（会制造观测缺口），被拒绝
  assert.throws(() => c.commitTakeover('sensor-2', 5), (e) => {
    assert.equal(e.code, 'BOUNDARY_GAP');
    return true;
  });
  // 拒绝后归属不变，仍由旧版负责
  assert.equal(c.snapshot('sensor-2').owner, 'old');

  // 合法接管后，新版不得对旧版拥有的序号提交处置
  c.reportOldWatermark('sensor-2', 4);
  c.commitTakeover('sensor-2', 5);
  assert.throws(() => c.submitDisposition('sensor-2', 4, { by: 'new', decision: 'blocked' }), (e) => {
    assert.equal(e.code, 'NOT_OWNER');
    return true;
  });

  // 重复序号与回退序号被拒绝
  assert.throws(() => c.ingest('sensor-2', 4), (e) => e.code === 'DUP_SEQ' || e.code === 'SEQ_REGRESSION');
});

test('边界/交错：新版断开回退到旧版可补齐边界，缺口单独拦截，已提交决定不变', () => {
  const c = new TakeoverCoordinator();
  c.registerSource('sensor-3', { firstSeq: 1 });
  for (const seq of [1, 2, 3, 4]) c.ingest('sensor-3', seq);
  c.reportOldWatermark('sensor-3', 4);
  c.reportOldBackfillLimit('sensor-3', 4);

  c.confirmNewReady('sensor-3');
  c.ingest('sensor-3', 5);
  c.ingest('sensor-3', 6);
  c.reportOldWatermark('sensor-3', 6);
  c.commitTakeover('sensor-3', 7);

  // 新版负责 7..10，但只提交了 7、8 的处置
  for (const seq of [7, 8, 9, 10]) c.ingest('sensor-3', seq);
  c.submitDisposition('sensor-3', 7, { by: 'new', decision: 'blocked' });
  c.submitDisposition('sensor-3', 8, { by: 'new', decision: 'quarantined' });

  // 旧版实际只能补齐到 9；10 谁也补不齐
  c.reportOldBackfillLimit('sensor-3', 9);
  const fb = c.reportNewDisconnect('sensor-3');
  assert.equal(fb.fallbackBoundary, 10);
  assert.deepEqual(fb.reassigned, [9]);
  assert.deepEqual(fb.gaps, [{ from: 10, to: 10 }]);

  const snap = c.snapshot('sensor-3');
  assert.equal(snap.owner, 'old');
  // 新版已提交的 7、8 保持不变
  assert.equal(snap.commits.find((x) => x.seq === 7).disposition, 'blocked');
  assert.equal(snap.commits.find((x) => x.seq === 8).disposition, 'quarantined');
  // 9 重新划归旧版，10 被单独拦截为观测缺口
  assert.equal(snap.commits.find((x) => x.seq === 9).owner, 'old');
  assert.ok(!snap.commits.some((x) => x.seq === 10));
  assert.ok(snap.gaps.some((g) => g.from === 10 && g.reason === 'new-disconnect-unrecoverable'));

  // 交错到达的迟到旧回执：对 8 只补历史；对重划归旧版的 9 正常补录
  assert.equal(c.submitDisposition('sensor-3', 8, { by: 'old', decision: 'allowed' }).applied, false);
  assert.equal(c.snapshot('sensor-3').commits.find((x) => x.seq === 8).disposition, 'quarantined');
  assert.equal(c.submitDisposition('sensor-3', 9, { by: 'old', decision: 'escalated' }).applied, true);

  // 回退后新事件由旧版接收负责；覆盖性核验通过（10 已被缺口拦截）
  assert.equal(c.ingest('sensor-3', 11).committedTo, 'old');
  const cov = c.verifyCoverage('sensor-3');
  assert.equal(cov.ok, true);
  assert.deepEqual(cov.uncovered, []);
});
