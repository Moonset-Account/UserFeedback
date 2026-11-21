import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Cluster } from '../src/cluster.mjs';
import { Node } from '../src/node.mjs';
import { keysForSlot } from '../src/hash.mjs';
import {
  ImportConflictError,
  OneTimeTicketRejectedError,
  WrongSlotStateError,
} from '../src/protocol.mjs';

function buildCluster(slot) {
  const cluster = new Cluster();
  cluster.addNode(new Node('A'));
  cluster.addNode(new Node('B'));
  cluster.assignSlot(slot, 'A');
  return cluster;
}

/** 在某节点上直接预置底层数据，绕过路由准入（仅合成数据用）。 */
function seedLocal(cluster, nodeId, entries) {
  const node = cluster.getNode(nodeId);
  for (const [key, value] of entries) {
    node.putLocal(key, value);
  }
}

test('搬迁全生命周期：begin -> 逐键 migrateKey -> complete，槽表最后才切换', () => {
  const slot = 321;
  const cluster = buildCluster(slot);
  const [k1, k2, k3] = keysForSlot(slot, 3);
  cluster.seed('A', [
    [k1, 'a'],
    [k2, 'b'],
    [k3, 'c'],
  ]);

  assert.equal(cluster.ownerOf(slot), 'A');
  cluster.beginMigration(slot, 'A', 'B');
  // 搬迁期间全局槽表仍指向 A
  assert.equal(cluster.ownerOf(slot), 'A');
  assert.equal(cluster.getNode('A').stateOf(slot), 'migrating');
  assert.equal(cluster.getNode('B').stateOf(slot), 'importing');

  cluster.migrateKey(slot, k1);
  cluster.migrateKey(slot, k2);
  const progress = cluster.migrationProgress(slot);
  assert.deepEqual(progress.migrated, [k1, k2]);
  assert.equal(progress.remainingOnSource, 1);

  // 还有源键，显式切换被拒绝
  assert.throws(() => cluster.completeMigration(slot), WrongSlotStateError);

  cluster.migrateKey(slot, k3);
  const switched = cluster.completeMigration(slot);
  assert.deepEqual(switched, { slot, newOwner: 'B' });

  assert.equal(cluster.ownerOf(slot), 'B');
  assert.equal(cluster.getNode('A').stateOf(slot), 'foreign');
  assert.equal(cluster.getNode('B').stateOf(slot), 'owner');
  assert.equal(cluster.getNode('B').slotKeyCount(slot), 3);
  assert.equal(cluster.getNode('A').slotKeyCount(slot), 0);
});

test('migrateRemainingKeys：逐键原子搬空整个槽', () => {
  const slot = 555;
  const cluster = buildCluster(slot);
  const keys = keysForSlot(slot, 6);
  cluster.seed('A', keys.map((k, i) => [k, i]));
  cluster.beginMigration(slot, 'A', 'B');

  const moved = cluster.migrateRemainingKeys(slot);
  assert.deepEqual(moved.sort(), [...keys].sort());
  assert.equal(cluster.getNode('A').slotKeyCount(slot), 0);
  assert.equal(cluster.getNode('B').slotKeyCount(slot), 6);
  cluster.completeMigration(slot);
  assert.equal(cluster.ownerOf(slot), 'B');
});

test('边界：空槽搬迁，begin 后可直接 complete，槽主切到 B', () => {
  const slot = 777;
  const cluster = buildCluster(slot);
  cluster.beginMigration(slot, 'A', 'B');
  assert.equal(cluster.getNode('A').slotKeyCount(slot), 0);
  const switched = cluster.completeMigration(slot);
  assert.equal(switched.newOwner, 'B');
  assert.equal(cluster.ownerOf(slot), 'B');
});

test('原子性：目标导入冲突时 migrateKey 抛错且键留在 A，进度不含该键', () => {
  const slot = 888;
  const cluster = buildCluster(slot);
  const [key] = keysForSlot(slot, 1);
  seedLocal(cluster, 'A', [[key, 'from-A']]);
  cluster.beginMigration(slot, 'A', 'B');
  // B 已在 IMPORTING：凭一张正规迁入许可预置同键，制造导入冲突
  const presetGrant = cluster.getNode('B').issueImportGrant(slot);
  cluster.getNode('B').handle({
    op: 'importKey',
    slot,
    key,
    value: 'already-on-B',
    ticket: presetGrant,
  });

  assert.throws(() => cluster.migrateKey(slot, key), ImportConflictError);
  assert.equal(cluster.getNode('A').hasKey(key), true);
  assert.equal(cluster.getNode('A').slotKeyCount(slot), 1);
  assert.deepEqual(cluster.migrationProgress(slot).migrated, []);
});

test('边界：重复搬迁同一个键第二次被拒（已不在源节点）', () => {
  const slot = 999;
  const cluster = buildCluster(slot);
  const [key] = keysForSlot(slot, 1);
  cluster.seed('A', [[key, 'v']]);
  cluster.beginMigration(slot, 'A', 'B');
  cluster.migrateKey(slot, key);
  assert.throws(() => cluster.migrateKey(slot, key), WrongSlotStateError);
});

test('边界：未开始搬迁就 migrateKey/complete 被拒；源目标相同被拒', () => {
  const slot = 1010;
  const cluster = buildCluster(slot);
  const [key] = keysForSlot(slot, 1);
  assert.throws(() => cluster.migrateKey(slot, key), WrongSlotStateError);
  assert.throws(() => cluster.completeMigration(slot), WrongSlotStateError);
  assert.throws(() => cluster.beginMigration(slot, 'A', 'A'), WrongSlotStateError);
});

test('边界：非法槽号分配抛 RangeError', () => {
  const cluster = new Cluster();
  cluster.addNode(new Node('A'));
  assert.throws(() => cluster.assignSlot(-1, 'A'), RangeError);
  assert.throws(() => cluster.assignSlot(16384, 'A'), RangeError);
});

test('搬过去又删除的键归 B：A 端 emigrated 记录保留，B 端删除后 A 仍不持有', () => {
  const slot = 1212;
  const cluster = buildCluster(slot);
  const [key] = keysForSlot(slot, 1);
  cluster.seed('A', [[key, 'v']]);
  cluster.beginMigration(slot, 'A', 'B');
  cluster.migrateKey(slot, key);
  assert.deepEqual(cluster.getNode('A').emigratedKeys(slot), [key]);
  assert.deepEqual(cluster.getNode('B').importedKeys(slot), [key]);

  // 直接在 B 上删除（模拟客户端通过 ASK 通道完成的删除）
  const B = cluster.getNode('B');
  const ask = cluster.getNode('A').issueAskTicket(slot);
  B.handle({ op: 'delete', slot, key, ticket: ask });
  assert.equal(B.hasKey(key), false);
  assert.equal(cluster.getNode('A').hasKey(key), false);
  assert.throws(
    () => cluster.migrateKey(slot, key),
    WrongSlotStateError, // A 已不持有，不能再搬
  );
});

test('迁入许可严格一次性：协调器一次 migrateKey 消费一张许可', () => {
  const slot = 1313;
  const cluster = buildCluster(slot);
  const [k1, k2] = keysForSlot(slot, 2);
  cluster.seed('A', [
    [k1, 1],
    [k2, 2],
  ]);
  cluster.beginMigration(slot, 'A', 'B');
  const grant = cluster.getNode('B').issueImportGrant(slot);
  // 手动用第一张许可搬 k1
  cluster.getNode('A').exportKey(slot, k1, (key, value) => {
    cluster.getNode('B').handle({ op: 'importKey', slot, key, value, ticket: grant });
  });
  assert.equal(grant.used, true);
  // 复用即拒
  assert.throws(
    () =>
      cluster.getNode('B').handle({ op: 'importKey', slot, key: k2, value: 2, ticket: grant }),
    OneTimeTicketRejectedError,
  );
  // k2 仍可通过正规流程搬迁
  cluster.migrateKey(slot, k2);
  assert.equal(cluster.getNode('B').hasKey(k2), true);
});
