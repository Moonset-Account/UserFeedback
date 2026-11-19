// tests/shared-write-check.test.mjs
//
// 验收用例（node:test）。覆盖：
//   - 可接受结果：内容相同的独立对象、全冻结的共享普通数据子图；
//   - 应被阻止/拒绝的结果：可写共享对象、冻结父节点+可写后代、非普通对象共享；
//   - 边界 / 交错场景：循环引用收敛、最短路径（菱形图）、访问器、代理、
//     symbol 键与不可枚举属性、null 原型、原始值根等；
//   - 计数替身：用计数 getter 与全 trap 计数 Proxy 核对核验过程本身
//     没有执行 getter、没有触发任何代理回调。

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { types as nodeUtilTypes } from 'node:util';

import {
  verifySharedWritable,
  formatPath,
  formatSegment,
} from '../src/shared-write-check.mjs';

/* ----------------------------- 测试替身 ----------------------------- */

/**
 * 计数访问器：每次 get/set 都会自增。核验若误触发 getter，
 * getCalls 就会大于 0，测试据此失败。
 */
function makeCountingAccessor(initialValue) {
  const counters = { getCalls: 0, setCalls: 0 };
  const holder = {};
  Object.defineProperty(holder, 'guarded', {
    enumerable: true,
    configurable: true,
    get() {
      counters.getCalls += 1;
      return initialValue;
    },
    set(v) {
      counters.setCalls += 1;
      initialValue = v;
    },
  });
  return { holder, counters };
}

/**
 * 全 trap 计数代理：每一种可被元数据/观测操作触发的 trap 都计数。
 * 核验只允许使用引擎内部槽级别的识别（util.types.isProxy），
 * 任何 trap 计数 > 0 都说明实现错误地触发了业务回调。
 */
function makeCountingProxy(target) {
  const counts = {
    getPrototypeOf: 0,
    setPrototypeOf: 0,
    isExtensible: 0,
    preventExtensions: 0,
    getOwnPropertyDescriptor: 0,
    defineProperty: 0,
    has: 0,
    get: 0,
    set: 0,
    deleteProperty: 0,
    ownKeys: 0,
    apply: 0,
    construct: 0,
  };
  const proxy = new Proxy(target, {
    getPrototypeOf(...a) {
      counts.getPrototypeOf += 1;
      return Reflect.getPrototypeOf(...a);
    },
    setPrototypeOf(...a) {
      counts.setPrototypeOf += 1;
      return Reflect.setPrototypeOf(...a);
    },
    isExtensible(...a) {
      counts.isExtensible += 1;
      return Reflect.isExtensible(...a);
    },
    preventExtensions(...a) {
      counts.preventExtensions += 1;
      return Reflect.preventExtensions(...a);
    },
    getOwnPropertyDescriptor(...a) {
      counts.getOwnPropertyDescriptor += 1;
      return Reflect.getOwnPropertyDescriptor(...a);
    },
    defineProperty(...a) {
      counts.defineProperty += 1;
      return Reflect.defineProperty(...a);
    },
    has(...a) {
      counts.has += 1;
      return Reflect.has(...a);
    },
    get(...a) {
      counts.get += 1;
      return Reflect.get(...a);
    },
    set(...a) {
      counts.set += 1;
      return Reflect.set(...a);
    },
    deleteProperty(...a) {
      counts.deleteProperty += 1;
      return Reflect.deleteProperty(...a);
    },
    ownKeys(...a) {
      counts.ownKeys += 1;
      return Reflect.ownKeys(...a);
    },
    apply(...a) {
      counts.apply += 1;
      return Reflect.apply(...a);
    },
    construct(...a) {
      counts.construct += 1;
      return Reflect.construct(...a);
    },
  });
  return { proxy, counts, total: () => Object.values(counts).reduce((a, b) => a + b, 0) };
}

/** 递归冻结（deepFreeze），用于构造可接受的共享冻结图；支持循环引用。 */
function deepFreeze(value, seen = new Set()) {
  if (value === null || typeof value !== 'object') return value;
  if (seen.has(value)) return value;
  seen.add(value);
  if (nodeUtilTypes.isProxy(value)) throw new Error('cannot freeze proxy in fixture');
  for (const key of Reflect.ownKeys(value)) {
    const d = Object.getOwnPropertyDescriptor(value, key);
    if (d && 'value' in d) deepFreeze(d.value, seen);
  }
  return Object.freeze(value);
}

const findViolation = (report, identity) =>
  report.violations.find((v) => v.identity === identity);

/* ----------------------------- 用例 ----------------------------- */

test('可接受：内容相同但相互独立的对象图通过核验', () => {
  const host = { a: 1, b: [1, 2, { c: 3 }], d: { e: 'x' } };
  const copy = { a: 1, b: [1, 2, { c: 3 }], d: { e: 'x' } };

  const report = verifySharedWritable(host, copy);

  assert.equal(report.ok, true);
  assert.deepEqual(report.violations, []);
  assert.deepEqual(report.unverifiable, []);
  assert.deepEqual(report.accepted, []);
});

test('应拒绝：可写共享对象被报告，且给出两侧最短路径', () => {
  const shared = { secret: 'mutable' };
  const host = { outer: { inner: shared } };
  const copy = { placed: shared };

  const report = verifySharedWritable(host, copy);

  assert.equal(report.ok, false);
  assert.equal(report.violations.length, 1);
  const v = report.violations[0];
  assert.equal(v.identity, shared);
  assert.equal(v.reason, 'not-frozen');
  assert.deepEqual(v.hostPath, ['outer', 'inner']);
  assert.deepEqual(v.copyPath, ['placed']);
  assert.equal(v.hostPathString, '<root>.outer.inner');
  assert.equal(v.copyPathString, '<root>.placed');
});

test('可接受：全冻结的共享普通数据子图（含循环引用）通过', () => {
  const node1 = { name: 'a' };
  const node2 = { name: 'b', back: node1 };
  node1.peer = node2;
  deepFreeze(node1);
  deepFreeze(node2);

  const host = { refs: [node1, node2] };
  const copy = { same: node2 };

  const report = verifySharedWritable(host, copy);

  assert.deepEqual(report.violations, []);
  assert.deepEqual(report.unverifiable, []);
  const identities = report.accepted.map((p) => p.identity).sort();
  assert.ok(identities.includes(node1));
  assert.ok(identities.includes(node2));
  // 两侧路径都可达 node2
  const node2Pair = report.accepted.find((p) => p.identity === node2);
  assert.deepEqual(node2Pair.hostPath, ['refs', '1']);
  assert.deepEqual(node2Pair.copyPath, ['same']);
});

test('应拒绝：共享父节点已冻结，但存在未冻结后代', () => {
  const child = { n: 1 }; // 不冻结
  const shared = Object.freeze({ child });
  const host = { shared };
  const copy = { shared };

  const report = verifySharedWritable(host, copy);

  assert.equal(report.ok, false);
  const v = findViolation(report, shared);
  assert.ok(v);
  assert.equal(v.reason, 'not-frozen');
  assert.deepEqual(v.relativePath, ['child']);
  assert.equal(v.relativePathString, '<root>.child');
});

test('边界：循环引用必须收敛终止，且共享循环图被正确识别', () => {
  const a = { tag: 'a' };
  const b = { tag: 'b' };
  a.b = b;
  b.a = a; // 循环

  const host = { start: a };
  const copy = { loop: b };

  const report = verifySharedWritable(host, copy);

  assert.equal(report.ok, false);
  const va = findViolation(report, a);
  const vb = findViolation(report, b);
  assert.ok(va && vb);
  assert.deepEqual(va.hostPath, ['start']);
  assert.deepEqual(va.copyPath, ['loop', 'a']);
  assert.deepEqual(vb.hostPath, ['start', 'b']);
  assert.deepEqual(vb.copyPath, ['loop']);
});

test('边界：菱形图中输出最短路径（BFS 首次到达）', () => {
  // host: root -> top -> leaf（短路径 root.top.leaf）
  //             root -> alt -> leaf（长路径 root.alt.leaf）
  const leaf = { k: 1 };
  const top = { leaf };
  const alt = { leaf };
  const host = { top, alt };
  const copy = { destination: leaf };

  const report = verifySharedWritable(host, copy);
  const v = findViolation(report, leaf);

  assert.ok(v);
  assert.deepEqual(v.hostPath, ['top', 'leaf']); // 短路径胜出
  assert.deepEqual(v.copyPath, ['destination']);
});

test('访问器：不执行 getter，分支标记为无法核验（计数替身核对）', () => {
  const { holder, counters } = makeCountingAccessor('do-not-read');
  const host = { section: holder };
  const copy = { section: { guarded: 'independent' } };

  const report = verifySharedWritable(host, copy);

  assert.equal(report.ok, false);
  assert.equal(counters.getCalls, 0, '核验过程不得触发访问器 getter');
  assert.equal(counters.setCalls, 0);
  assert.equal(report.unverifiable.length, 1);
  assert.equal(report.unverifiable[0].side, 'host');
  assert.equal(report.unverifiable[0].reason, 'accessor');
  assert.deepEqual(report.unverifiable[0].path, ['section', 'guarded']);
  assert.deepEqual(report.violations, []); // 没有对象身份被共享
});

test('代理：不触发任何代理回调，分支标记为无法核验（全 trap 计数核对）', () => {
  const { proxy, total } = makeCountingProxy({ a: 1, nested: { b: 2 } });
  assert.equal(nodeUtilTypes.isProxy(proxy), true); // 前置：确实是代理

  const host = { pluginView: proxy };
  const copy = { pluginView: { a: 1, nested: { b: 2 } } };

  const report = verifySharedWritable(host, copy);

  assert.equal(total(), 0, '核验过程不得触发任何代理 trap');
  assert.equal(report.ok, false);
  assert.equal(report.unverifiable.length, 1);
  assert.equal(report.unverifiable[0].reason, 'proxy');
  assert.equal(report.unverifiable[0].side, 'host');
  assert.deepEqual(report.unverifiable[0].path, ['pluginView']);
  // 代理内部对象不得被展开，因此不存在共享身份
  assert.deepEqual(report.violations, []);
});

test('代理作为根：整根无法核验且零 trap', () => {
  const { proxy, total } = makeCountingProxy({ x: 1 });
  const report = verifySharedWritable(proxy, { x: 1 });

  assert.equal(total(), 0);
  assert.equal(report.ok, false);
  assert.equal(report.unverifiable[0].reason, 'root-proxy');
  assert.equal(report.unverifiable[0].side, 'host');
});

test('交错场景：同一图中既有可接受的冻结共享，也有可写共享与无法核验分支', () => {
  const frozenShared = deepFreeze({ ok: { deep: 1 } });
  const mutableShared = { m: 1 };
  const { holder, counters } = makeCountingAccessor(123);

  const host = { frozen: frozenShared, bad: mutableShared, gated: holder };
  const copy = { frozen: frozenShared, bad: { renamed: mutableShared } };

  const report = verifySharedWritable(host, copy);

  assert.equal(report.ok, false);
  // 可写共享被拒绝
  const v = findViolation(report, mutableShared);
  assert.ok(v);
  assert.deepEqual(v.hostPath, ['bad']);
  assert.deepEqual(v.copyPath, ['bad', 'renamed']);
  // 冻结共享被接受（含其冻结后代）
  const acceptedIds = report.accepted.map((p) => p.identity);
  assert.ok(acceptedIds.includes(frozenShared));
  assert.ok(acceptedIds.includes(frozenShared.ok));
  // 访问器分支无法核验，且 getter 未执行
  assert.equal(counters.getCalls, 0);
  assert.ok(report.unverifiable.some((u) => u.reason === 'accessor'));
});

test('非普通对象（类实例/内建对象）共享一律拒绝，即使冻结', () => {
  class Box {
    constructor(v) {
      this.v = v;
    }
  }
  const frozenBox = Object.freeze(new Box(1));
  const frozenMap = Object.freeze(new Map([['k', 1]]));

  const report1 = verifySharedWritable({ box: frozenBox }, { box: frozenBox });
  assert.equal(report1.ok, false);
  assert.equal(findViolation(report1, frozenBox).reason, 'non-plain-object');

  const report2 = verifySharedWritable({ map: frozenMap }, { map: frozenMap });
  assert.equal(report2.ok, false);
  assert.equal(findViolation(report2, frozenMap).reason, 'non-plain-object');
});

test('冻结共享子图中出现异构后代时拒绝', () => {
  const date = new Date(0);
  const shared = Object.freeze({ when: date });
  const report = verifySharedWritable({ shared }, { shared });

  const v = findViolation(report, shared);
  assert.ok(v);
  assert.equal(v.reason, 'non-plain-descendant');
  assert.deepEqual(v.relativePath, ['when']);
});

test('symbol 键与不可枚举数据属性也被遍历，路径保留 symbol 段', () => {
  const sym = Symbol('slot');
  const shared = { visible: 1 };
  Object.defineProperty(shared, 'hidden', {
    value: { deep: shared }, // 循环回自身
    enumerable: false,
    writable: true,
    configurable: true,
  });
  shared[sym] = { leaf: 1 };

  const host = { root: shared };
  const copy = { root: shared };

  const report = verifySharedWritable(host, copy);
  const v = findViolation(report, shared);
  assert.ok(v);

  // symbol 路径段可被格式化
  const symChild = report.hostPaths.get(shared[sym]);
  assert.ok(symChild);
  assert.equal(typeof symChild.path[1], 'symbol');
  assert.equal(formatPath(symChild.path), '<root>.root[Symbol(slot)]');
  assert.equal(formatSegment(sym), '[Symbol(slot)]');
});

test('null 原型对象视为普通对象，可参与冻结共享', () => {
  const nullProto = Object.create(null);
  nullProto.value = 1;
  Object.freeze(nullProto);

  const report = verifySharedWritable({ x: nullProto }, { x: nullProto });
  assert.deepEqual(report.violations, []);
  assert.ok(report.accepted.some((p) => p.identity === nullProto));
});

test('原始值 / null 根：无对象身份，直接通过', () => {
  for (const [h, c] of [
    [1, 1],
    ['s', 's'],
    [null, null],
    [undefined, { a: 1 }],
    [true, false],
  ]) {
    const report = verifySharedWritable(h, c);
    assert.equal(report.ok, true, `根 ${String(h)} / ${String(c)} 应通过`);
    assert.deepEqual(report.violations, []);
  }
});

test('数组下标路径使用方括号渲染', () => {
  const shared = [{ k: 1 }];
  const report = verifySharedWritable({ list: shared }, { list: shared });
  const v = findViolation(report, shared[0]);
  assert.equal(v.hostPathString, '<root>.list[0]');
  const arrayViolation = findViolation(report, shared);
  assert.equal(arrayViolation.hostPathString, '<root>.list');
});

test('函数值：不调用、不展开，标记为无法核验', () => {
  const fn = () => 1;
  const report = verifySharedWritable({ f: fn }, { f: fn });

  assert.equal(report.ok, false);
  assert.deepEqual(report.violations, []); // 函数不作为可写共享对象上报
  const reasons = report.unverifiable.map((u) => u.reason).sort();
  assert.deepEqual(reasons, ['function', 'function']);
  assert.ok(report.unverifiable.every((u) => u.path[0] === 'f'));
});
