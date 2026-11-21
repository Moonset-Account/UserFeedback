import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Node } from '../src/node.mjs';
import { keySlot, keysForSlot } from '../src/hash.mjs';
import {
  AskRedirect,
  AskTicket,
  ImportConflictError,
  ImportGrant,
  MovedRedirect,
  OneTimeTicketRejectedError,
  TryAgain,
  WrongSlotStateError,
} from '../src/protocol.mjs';

const SLOT = 100;
const OTHER_SLOT = 200;

function makePair() {
  const A = new Node('A');
  const B = new Node('B');
  A.bindOwnerResolver(() => 'B');
  B.bindOwnerResolver(() => 'A');
  A.assignSlot(SLOT);
  return { A, B };
}

function seed(node, entries) {
  for (const [key, value] of entries) {
    node.putLocal(key, value);
  }
}

test('正常态：槽主节点直接服务 get/set/delete/mget', () => {
  const { A } = makePair();
  const key = keysForSlot(SLOT, 1)[0];
  const setRes = A.handle({ op: 'set', slot: SLOT, key, value: 'v1' });
  assert.equal(setRes.stored, true);
  const getRes = A.handle({ op: 'get', slot: SLOT, key });
  assert.deepEqual(getRes.value, 'v1');
  const delRes = A.handle({ op: 'delete', slot: SLOT, key });
  assert.equal(delRes.existed, true);
  const miss = A.handle({ op: 'get', slot: SLOT, key });
  assert.equal(miss.exists, false);
  assert.equal(miss.value, null);
});

test('MIGRATING：尚留在 A 的键仍由 A 服务', () => {
  const { A, B } = makePair();
  A.beginMigrating(SLOT, 'B');
  B.beginImporting(SLOT, 'A');
  const [k1] = keysForSlot(SLOT, 1);
  seed(A, [[k1, 'stay']]);
  const res = A.handle({ op: 'get', slot: SLOT, key: k1 });
  assert.equal(res.nodeId, 'A');
  assert.equal(res.value, 'stay');
});

test('MIGRATING：A 上不存在的键（含新建）返回一次性 ASK 到 B', () => {
  const { A, B } = makePair();
  A.beginMigrating(SLOT, 'B');
  B.beginImporting(SLOT, 'A');
  const [newKey] = keysForSlot(SLOT, 1);

  assert.throws(
    () => A.handle({ op: 'get', slot: SLOT, key: newKey }),
    (err) => err instanceof AskRedirect && err.targetNodeId === 'B',
  );
  assert.throws(
    () => A.handle({ op: 'set', slot: SLOT, key: newKey, value: 'x' }),
    AskRedirect,
  );
});

test('MIGRATING：搬走后又在 B 删除的键，回到 A 仍归 B（ASK，而不是 A 本地 miss）', () => {
  const { A, B } = makePair();
  A.beginMigrating(SLOT, 'B');
  B.beginImporting(SLOT, 'A');
  const [key] = keysForSlot(SLOT, 1);
  seed(A, [[key, 'v']]);

  const grant = B.issueImportGrant(SLOT);
  A.exportKey(SLOT, key, (k, value) => {
    B.handle({ op: 'importKey', slot: SLOT, key: k, value, ticket: grant });
  });
  // B 上删除
  const ask = A.issueAskTicket(SLOT);
  B.handle({ op: 'delete', slot: SLOT, key, ticket: ask });
  assert.equal(B.hasKey(key), false);

  // A 本地没有该键 -> 依旧 ASK 到 B，由 B 决定 miss
  assert.throws(() => A.handle({ op: 'get', slot: SLOT, key }), AskRedirect);
});

test('MIGRATING 多键读：全部在 A -> 正常返回；全部归 B -> ASK；两侧都有 -> TRYAGAIN 且无部分值', () => {
  const { A, B } = makePair();
  A.beginMigrating(SLOT, 'B');
  B.beginImporting(SLOT, 'A');
  const [k1, k2, k3, k4] = keysForSlot(SLOT, 4);
  seed(A, [
    [k1, 1],
    [k2, 2],
    [k3, 3],
    [k4, 4],
  ]);

  // 全部在 A
  const all = A.handle({ op: 'mget', slot: SLOT, keys: [k1, k2] });
  assert.equal(all.values.length, 2);

  // 搬走 k1、k2 到 B
  for (const k of [k1, k2]) {
    const grant = B.issueImportGrant(SLOT);
    A.exportKey(SLOT, k, (key, value) => {
      B.handle({ op: 'importKey', slot: SLOT, key, value, ticket: grant });
    });
  }

  // 全部归 B：ASK
  assert.throws(() => A.handle({ op: 'mget', slot: SLOT, keys: [k1, k2] }), AskRedirect);

  // 两侧都有：TRYAGAIN，错误上携带两侧键且结果信封根本不存在
  let caught;
  try {
    A.handle({ op: 'mget', slot: SLOT, keys: [k1, k2, k3, k4] });
  } catch (err) {
    caught = err;
  }
  assert.ok(caught instanceof TryAgain);
  assert.deepEqual([...caught.localKeys].sort(), [k3, k4].sort());
  assert.deepEqual([...caught.remoteKeys].sort(), [k1, k2].sort());
  assert.equal(caught.values, undefined);
});

test('IMPORTING：B 拒绝无票据/错票据请求，只凭一次性 ASK 指示服务一次', () => {
  const { A, B } = makePair();
  A.beginMigrating(SLOT, 'B');
  B.beginImporting(SLOT, 'A');
  const [key] = keysForSlot(SLOT, 1);

  // 无票据
  assert.throws(() => B.handle({ op: 'get', slot: SLOT, key }), OneTimeTicketRejectedError);

  // 拿到一次性指示，第一次成功（miss 也算 B 处理）
  const ticket = A.issueAskTicket(SLOT);
  const res = B.handle({ op: 'get', slot: SLOT, key, ticket });
  assert.equal(res.nodeId, 'B');
  assert.equal(res.exists, false);

  // 票据已消费，复用被拒
  assert.throws(
    () => B.handle({ op: 'get', slot: SLOT, key, ticket }),
    OneTimeTicketRejectedError,
  );
});

test('IMPORTING：B 只凭一次性迁入许可接受 importKey，许可不可复用、不可拿 ASK 票据顶替', () => {
  const { A, B } = makePair();
  A.beginMigrating(SLOT, 'B');
  B.beginImporting(SLOT, 'A');
  const [key] = keysForSlot(SLOT, 1);

  // 无许可
  assert.throws(
    () => B.handle({ op: 'importKey', slot: SLOT, key, value: 'v' }),
    OneTimeTicketRejectedError,
  );

  // ASK 票据不能当迁入许可
  const ask = A.issueAskTicket(SLOT);
  assert.throws(
    () => B.handle({ op: 'importKey', slot: SLOT, key, value: 'v', ticket: ask }),
    OneTimeTicketRejectedError,
  );

  // 正规许可：导入成功
  const grant = B.issueImportGrant(SLOT);
  B.handle({ op: 'importKey', slot: SLOT, key, value: 'v', ticket: grant });
  assert.equal(B.hasKey(key), true);

  // 许可已焚，不能搬第二个键
  assert.throws(
    () => B.handle({ op: 'importKey', slot: SLOT, key: 'another', value: 'w', ticket: grant }),
    OneTimeTicketRejectedError,
  );
});

test('IMPORTING：目标已存在同键时导入冲突，且不覆盖原值', () => {
  const { A, B } = makePair();
  A.beginMigrating(SLOT, 'B');
  B.beginImporting(SLOT, 'A');
  const [key] = keysForSlot(SLOT, 1);

  const g1 = B.issueImportGrant(SLOT);
  B.handle({ op: 'importKey', slot: SLOT, key, value: 'first', ticket: g1 });

  const g2 = B.issueImportGrant(SLOT);
  assert.throws(
    () => B.handle({ op: 'importKey', slot: SLOT, key, value: 'second', ticket: g2 }),
    ImportConflictError,
  );
  const ask = A.issueAskTicket(SLOT);
  const res = B.handle({ op: 'get', slot: SLOT, key, ticket: ask });
  assert.equal(res.value, 'first');
});

test('原子性：B 导入失败时 A 不删键，键原样留在源节点', () => {
  const { A, B } = makePair();
  A.beginMigrating(SLOT, 'B');
  B.beginImporting(SLOT, 'A');
  const [key] = keysForSlot(SLOT, 1);
  seed(A, [[key, 'original']]);

  // 用一张不属于 B 的伪造许可（目标 id 不匹配）触发拒绝
  const bogus = new ImportGrant(SLOT, 'X');
  assert.throws(
    () =>
      A.exportKey(SLOT, key, (k, value) => {
        B.handle({ op: 'importKey', slot: SLOT, key: k, value, ticket: bogus });
      }),
    OneTimeTicketRejectedError,
  );
  assert.equal(A.hasKey(key), true);
  assert.equal(B.hasKey(key), false);
  assert.equal(A.handle({ op: 'get', slot: SLOT, key }).value, 'original');
});

test('票据错槽/错节点使用同样被拒并作废', () => {
  const { A, B } = makePair();
  A.beginMigrating(SLOT, 'B');
  B.beginImporting(SLOT, 'A');
  const ticket = new AskTicket(OTHER_SLOT, 'B');
  assert.throws(
    () => B.handle({ op: 'get', slot: SLOT, key: 'x', ticket }),
    OneTimeTicketRejectedError,
  );
  assert.equal(ticket.used, true);
});

test('显式切换槽主后：A 对该槽一律 MOVED（永久），B 成为槽主正常服务且不再要票据', () => {
  const { A, B } = makePair();
  A.beginMigrating(SLOT, 'B');
  B.beginImporting(SLOT, 'A');
  const [key] = keysForSlot(SLOT, 1);
  seed(A, [[key, 'v']]);
  const grant = B.issueImportGrant(SLOT);
  A.exportKey(SLOT, key, (k, value) => {
    B.handle({ op: 'importKey', slot: SLOT, key: k, value, ticket: grant });
  });

  // 还有源键时拒绝提前切换（边界：本槽恰好已搬空，直接验证切换流程）
  A.completeMigration(SLOT);
  B.completeImport(SLOT);

  assert.throws(
    () => A.handle({ op: 'get', slot: SLOT, key }),
    (err) => err instanceof MovedRedirect && err.targetNodeId === 'B',
  );
  // 无需任何票据
  const res = B.handle({ op: 'get', slot: SLOT, key });
  assert.equal(res.value, 'v');
  assert.equal(B.stateOf(SLOT), 'owner');
  assert.equal(A.stateOf(SLOT), 'foreign');
});

test('边界：MIGRATING 仍有源键时 completeMigration 被拒绝', () => {
  const { A, B } = makePair();
  A.beginMigrating(SLOT, 'B');
  B.beginImporting(SLOT, 'A');
  const [key] = keysForSlot(SLOT, 1);
  seed(A, [[key, 'v']]);
  assert.throws(() => A.completeMigration(SLOT), WrongSlotStateError);
});

test('边界：非 MIGRATING/IMPORTING 节点不能签发票据；槽主拒绝迁入、外节点给 MOVED', () => {
  const { A, B } = makePair();
  assert.throws(() => A.issueAskTicket(SLOT), WrongSlotStateError);
  assert.throws(() => B.issueImportGrant(SLOT), WrongSlotStateError);
  const grant = new ImportGrant(SLOT, 'B');
  // B 与该槽无关（foreign）：路由层直接永久重定向到槽主 A
  assert.throws(
    () => B.handle({ op: 'importKey', slot: SLOT, key: 'k', value: 1, ticket: grant }),
    MovedRedirect,
  );
  // A 是槽主：owner 状态不接受迁入请求
  assert.throws(
    () => A.handle({ op: 'importKey', slot: SLOT, key: 'k', value: 1, ticket: grant }),
    WrongSlotStateError,
  );
});
