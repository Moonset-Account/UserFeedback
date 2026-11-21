import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Cluster } from '../src/cluster.mjs';
import { Node } from '../src/node.mjs';
import { SmartClient } from '../src/client.mjs';
import { keySlot, keysForSlot } from '../src/hash.mjs';
import { CrossSlotError, TryAgain } from '../src/protocol.mjs';

function build(slot) {
  const cluster = new Cluster();
  cluster.addNode(new Node('A'));
  cluster.addNode(new Node('B'));
  cluster.assignSlot(slot, 'A');
  const client = new SmartClient(cluster);
  return { cluster, client, A: cluster.getNode('A'), B: cluster.getNode('B') };
}

test('正常态：请求直接命中缓存槽主 A，无重定向轨迹', () => {
  const slot = 11;
  const { client } = build(slot);
  const [key] = keysForSlot(slot, 1);
  const res = client.set(key, 'v');
  assert.equal(res.nodeId, 'A');
  assert.equal(res.routing.redirectedOnce, false);
  assert.equal(res.routing.slotTableUpdated, false);
  assert.equal(client.get(key).value, 'v');
});

test('ASK：搬迁中读已归 B 的键，只经一次性通道重试一次，槽表不变', () => {
  const slot = 22;
  const { cluster, client } = build(slot);
  const [movedKey, stayKey] = keysForSlot(slot, 2);
  cluster.seed('A', [
    [movedKey, 'mv'],
    [stayKey, 'st'],
  ]);
  cluster.beginMigration(slot, 'A', 'B');
  cluster.migrateKey(slot, movedKey);

  // 客户端缓存仍是旧槽表 A
  assert.equal(client.cachedOwner(slot), 'A');

  const res = client.get(movedKey);
  assert.equal(res.nodeId, 'B');
  assert.equal(res.value, 'mv');
  assert.equal(res.routing.redirectedOnce, true);
  assert.equal(res.routing.slotTableUpdated, false);
  // 轨迹：先发 A 收到 ASK，再走一次性通道到 B
  assert.equal(res.routing.attempts[0].nodeId, 'A');
  assert.equal(res.routing.attempts[0].outcome, 'ASK');
  assert.equal(res.routing.attempts[1].nodeId, 'B');
  assert.equal(res.routing.attempts[1].via, 'ASK-channel');
  // 槽表没有被改
  assert.equal(client.cachedOwner(slot), 'A');

  // 下一次请求仍然先发 A（一次性指示不残留）
  const res2 = client.get(movedKey);
  assert.equal(res2.routing.attempts[0].nodeId, 'A');
  assert.equal(res2.routing.redirectedOnce, true);
  assert.equal(client.stats.askRetries, 2);
});

test('ASK 通道用于新建：搬迁中 set 新键落在 B，槽表仍指 A', () => {
  const slot = 33;
  const { cluster, client, B } = build(slot);
  const [key] = keysForSlot(slot, 1);
  cluster.beginMigration(slot, 'A', 'B');

  const res = client.set(key, 'fresh');
  assert.equal(res.nodeId, 'B');
  assert.equal(res.routing.redirectedOnce, true);
  assert.equal(res.routing.slotTableUpdated, false);
  assert.equal(B.hasKey(key), true);
  assert.equal(client.cachedOwner(slot), 'A');
});

test('TRYAGAIN：同槽两组键分居两侧时 multiGet 暂缓，调用者拿不到任何部分值', () => {
  const slot = 44;
  const { cluster, client } = build(slot);
  const [k1, k2, k3, k4] = keysForSlot(slot, 4);
  cluster.seed('A', [
    [k1, 1],
    [k2, 2],
    [k3, 3],
    [k4, 4],
  ]);
  cluster.beginMigration(slot, 'A', 'B');
  cluster.migrateKey(slot, k1);
  cluster.migrateKey(slot, k2);

  let caught;
  try {
    client.multiGet([k1, k2, k3, k4]);
  } catch (err) {
    caught = err;
  }
  assert.ok(caught instanceof TryAgain);
  assert.equal(caught.values, undefined); // 绝无部分值
  assert.deepEqual(caught.localKeys.sort(), [k3, k4]);
  assert.deepEqual(caught.remoteKeys.sort(), [k1, k2]);
  // TRYAGAIN 不改槽表、不产生隐式重试
  assert.equal(client.cachedOwner(slot), 'A');
  assert.equal(caught.routing.slotTableUpdated, false);
});

test('全在 B 的多键读：ASK 一次性通道整组返回，不触发 TRYAGAIN', () => {
  const slot = 55;
  const { cluster, client } = build(slot);
  const [k1, k2] = keysForSlot(slot, 2);
  cluster.seed('A', [
    [k1, 'a'],
    [k2, 'b'],
  ]);
  cluster.beginMigration(slot, 'A', 'B');
  cluster.migrateKey(slot, k1);
  cluster.migrateKey(slot, k2);

  const res = client.multiGet([k1, k2]);
  assert.equal(res.nodeId, 'B');
  assert.equal(res.values.map((v) => v.value).join(','), 'a,b');
  assert.equal(res.routing.redirectedOnce, true);
  assert.equal(res.routing.slotTableUpdated, false);
});

test('MOVED：显式切换槽主后，客户端更新槽表并按新槽主重试，后续直发 B', () => {
  const slot = 66;
  const { cluster, client } = build(slot);
  const [key] = keysForSlot(slot, 1);
  cluster.seed('A', [[key, 'v']]);
  cluster.beginMigration(slot, 'A', 'B');
  cluster.migrateKey(slot, key);
  cluster.completeMigration(slot);

  // 客户端缓存仍是旧槽表 A
  assert.equal(client.cachedOwner(slot), 'A');
  const res = client.get(key);
  assert.equal(res.nodeId, 'B');
  assert.equal(res.value, 'v');
  assert.equal(res.routing.slotTableUpdated, true);
  assert.equal(res.routing.redirectedOnce, false);
  assert.equal(res.routing.attempts[0].outcome, 'MOVED');
  // 槽表已永久更新
  assert.equal(client.cachedOwner(slot), 'B');

  // 后续请求直发 B，不再经过 A
  const res2 = client.get(key);
  assert.equal(res2.routing.attempts.length, 1);
  assert.equal(res2.routing.attempts[0].nodeId, 'B');
  assert.equal(client.stats.movedRedirects, 1);
});

test('题面场景：用同一旧槽表读两组键 —— 搬迁中只重试一次(ASK)，切换后改槽表(MOVED)', () => {
  const slot = 77;
  const { cluster, client } = build(slot);
  const allGroupKeys = keysForSlot(slot, 5);
  const firstGroup = allGroupKeys.slice(0, 3);
  const secondGroup = allGroupKeys.slice(3);
  cluster.seed('A', allGroupKeys.map((k, i) => [k, i]));
  cluster.beginMigration(slot, 'A', 'B');
  for (const k of firstGroup) cluster.migrateKey(slot, k);

  // 此时 secondGroup 还在 A，firstGroup 已在 B —— 两组键归属不同节点
  // 读 firstGroup：全在 B -> ASK，只重试一次，槽表不动
  const askRes = client.multiGet(firstGroup);
  assert.equal(askRes.routing.redirectedOnce, true);
  assert.equal(askRes.routing.slotTableUpdated, false);
  assert.equal(client.cachedOwner(slot), 'A');

  // 读 secondGroup：全在 A -> 直接服务，无重定向
  const localRes = client.multiGet(secondGroup);
  assert.equal(localRes.nodeId, 'A');
  assert.equal(localRes.routing.attempts.length, 1);

  // 搬完剩余键并显式切换槽主
  cluster.migrateRemainingKeys(slot);
  cluster.completeMigration(slot);

  // 同一张旧槽表（切换前缓存）再读任意一组：MOVED -> 更新槽表
  const movedRes = client.multiGet(firstGroup);
  assert.equal(movedRes.routing.slotTableUpdated, true);
  assert.equal(movedRes.routing.redirectedOnce, false);
  assert.equal(client.cachedOwner(slot), 'B');

  // 调用者凭信封上的两个布尔位明确区分：一次性重试 vs 改槽表
  assert.deepEqual(
    { ask: askRes.routing.redirectedOnce, moved: askRes.routing.slotTableUpdated },
    { ask: true, moved: false },
  );
  assert.deepEqual(
    { ask: movedRes.routing.redirectedOnce, moved: movedRes.routing.slotTableUpdated },
    { ask: false, moved: true },
  );
});

test('交错场景：搬迁与客户端读写交替进行，路由结果始终与键当前归属一致', () => {
  const slot = 88;
  const { cluster, client } = build(slot);
  const keys = keysForSlot(slot, 4);
  cluster.seed('A', keys.map((k, i) => [k, `v${i}`]));
  cluster.beginMigration(slot, 'A', 'B');

  const trace = [];
  // 搬一个、读一轮，交错进行
  for (let i = 0; i < keys.length; i += 1) {
    cluster.migrateKey(slot, keys[i]);
    for (let j = 0; j < keys.length; j += 1) {
      const res = client.get(keys[j]);
      trace.push({ key: keys[j], servedBy: res.nodeId, viaAsk: res.routing.redirectedOnce });
      assert.equal(res.value, `v${j}`);
      if (j <= i) {
        assert.equal(res.nodeId, 'B');
        assert.equal(res.viaAsk ?? res.routing.redirectedOnce, true);
      } else {
        assert.equal(res.nodeId, 'A');
      }
    }
  }
  // 每次都没改槽表
  assert.equal(client.cachedOwner(slot), 'A');
  assert.ok(trace.length === keys.length * keys.length);

  cluster.migrateRemainingKeys(slot);
  cluster.completeMigration(slot);
  const after = client.get(keys[0]);
  assert.equal(after.nodeId, 'B');
  assert.equal(after.routing.slotTableUpdated, true);
});

test('搬走又删除的键：客户端经 ASK 通道在 B 删除，再读得到 B 的 miss 而非 A 的值', () => {
  const slot = 99;
  const { cluster, client } = build(slot);
  const [key] = keysForSlot(slot, 1);
  cluster.seed('A', [[key, 'ghost']]);
  cluster.beginMigration(slot, 'A', 'B');
  cluster.migrateKey(slot, key);

  const del = client.remove(key);
  assert.equal(del.nodeId, 'B');
  assert.equal(del.existed, true);
  assert.equal(del.routing.redirectedOnce, true);

  const get = client.get(key);
  assert.equal(get.nodeId, 'B');
  assert.equal(get.exists, false);
  assert.equal(get.value, null);
  // 始终没有改槽表
  assert.equal(client.cachedOwner(slot), 'A');
});

test('边界：multiGet 跨槽抛 CROSSSLOT；空键列表抛 TypeError', () => {
  const slot = 111;
  const { client } = build(slot);
  // 找两个不同槽的键
  let other = 'zzz';
  while (keySlot(other) === slot) other += 'z';
  const [sameSlotKey] = keysForSlot(slot, 1);
  assert.throws(() => client.multiGet([sameSlotKey, other]), CrossSlotError);
  assert.throws(() => client.multiGet([]), TypeError);
});

test('边界：客户端可持有落后于集群的旧槽表快照启动，MOVED 后自愈', () => {
  const slot = 222;
  const { cluster } = build(slot);
  const [key] = keysForSlot(slot, 1);
  cluster.seed('A', [[key, 'v']]);
  cluster.beginMigration(slot, 'A', 'B');
  cluster.migrateKey(slot, key);
  cluster.completeMigration(slot);

  // 用显式旧槽表（仍指 A）构造客户端
  const stale = new Map(cluster.slotTableSnapshot());
  stale.set(slot, 'A');
  const client = new SmartClient(cluster, stale);
  assert.equal(client.cachedOwner(slot), 'A');
  const res = client.get(key);
  assert.equal(res.routing.slotTableUpdated, true);
  assert.equal(client.cachedOwner(slot), 'B');
});

test('TRYAGAIN 后等待搬迁推进再重试：最终成功且无部分值泄漏', () => {
  const slot = 333;
  const { cluster, client } = build(slot);
  const [k1, k2] = keysForSlot(slot, 2);
  cluster.seed('A', [
    [k1, 1],
    [k2, 2],
  ]);
  cluster.beginMigration(slot, 'A', 'B');
  cluster.migrateKey(slot, k1); // 一侧一个 -> TRYAGAIN

  assert.throws(() => client.multiGet([k1, k2]), TryAgain);

  // 搬迁推进：k2 也搬走，两侧归属合并到 B
  cluster.migrateKey(slot, k2);
  const res = client.multiGet([k1, k2]);
  assert.equal(res.nodeId, 'B');
  assert.equal(res.values.length, 2);
  assert.equal(res.routing.redirectedOnce, true);
});
