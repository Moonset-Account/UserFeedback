import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { EventBus, matchLevels, validateFilter } from '../src/event-bus.js';

const split = (s) => s.split('.');

describe('层级主题匹配：* 一层、# 零层或多层', () => {
  const matches = (filter, topic) => matchLevels(split(topic), validateFilter(filter));

  it('精确匹配与单层 * 通配', () => {
    assert.equal(matches('order.created.eu', 'order.created.eu'), true);
    assert.equal(matches('order.created.eu', 'order.created.us'), false);
    assert.equal(matches('order.*.eu', 'order.created.eu'), true);
    assert.equal(matches('order.*.eu', 'order.paid.eu'), true);
    // * 只匹配一层，不能吞掉层级
    assert.equal(matches('order.*', 'order.created.eu'), false);
    assert.equal(matches('*.created', 'a.created'), true);
    assert.equal(matches('*.created', 'a.b.created'), false);
  });

  it('末尾 # 匹配零层或多层，且只允许出现在末尾', () => {
    assert.equal(matches('a.#', 'a'), true); // 零层
    assert.equal(matches('a.#', 'a.b'), true);
    assert.equal(matches('a.#', 'a.b.c'), true);
    assert.equal(matches('a.#', 'x.b.c'), false);
    assert.equal(matches('#', 'anything.at.all'), true); // 单独 # 匹配任意主题
    assert.equal(matches('a.b.#', 'a.b'), true);
    assert.equal(matches('a.b.#', 'a.c.d'), false);
  });

  it('非法过滤器在订阅阶段被拒绝', () => {
    assert.throws(() => validateFilter('a.#.b'), /# 只能位于过滤器末尾/);
    assert.throws(() => validateFilter('a..b'), /空层级/);
    assert.throws(() => validateFilter(''), /非法过滤器/);
    const bus = new EventBus();
    assert.throws(() => bus.subscribe('s1', 'a.#.b'), /# 只能位于过滤器末尾/);
    assert.throws(() => bus.publish('a.*.b'), /发布主题不得包含通配符/);
  });
});

describe('可接受的发布：普通订阅者合并、共享组轮转', () => {
  it('同一普通订阅者多条规则命中只投一份', () => {
    const bus = new EventBus();
    bus.subscribe('s1', 'order.*.eu');
    bus.subscribe('s1', 'order.created.#'); // 与上一条同时命中
    const r = bus.publish('order.created.eu', { n: 1 });
    assert.equal(r.status, 'accepted');
    assert.deepEqual(bus.inboxOf('s1'), [r.messageId]); // 只有一份
  });

  it('共享组按固定成员顺序、从上次成功者之后轮转，每组一份', () => {
    const bus = new EventBus();
    bus.createSharedGroup('g1', [{ id: 'm1' }, { id: 'm2' }, { id: 'm3' }]);
    bus.subscribeSharedGroup('g1', 'evt.#');
    assert.deepEqual(bus.memberOrderOf('g1'), ['m1', 'm2', 'm3']);

    const p1 = bus.publish('evt.a');
    const p2 = bus.publish('evt.b');
    const p3 = bus.publish('evt.c');
    assert.equal(p1.status, 'accepted');
    assert.equal(p2.status, 'accepted');
    assert.equal(p3.status, 'accepted');

    assert.deepEqual(bus.memberInboxOf('g1', 'm1'), [p1.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm2'), [p2.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm3'), [p3.messageId]);
    assert.equal(bus.cursorOf('g1'), 2); // 指针停在上次成功者 m3
  });

  it('普通订阅者与多个共享组同次发布各取所需', () => {
    const bus = new EventBus();
    bus.subscribe('s1', 'x.#');
    bus.createSharedGroup('g1', [{ id: 'a' }, { id: 'b' }]);
    bus.subscribeSharedGroup('g1', 'x.#');
    bus.createSharedGroup('g2', [{ id: 'c' }]);
    bus.subscribeSharedGroup('g2', 'x.*');
    const r = bus.publish('x.y');
    assert.equal(r.status, 'accepted');
    assert.deepEqual(bus.inboxOf('s1'), [r.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'a'), [r.messageId]);
    assert.deepEqual(bus.memberInboxOf('g2', 'c'), [r.messageId]);
    assert.deepEqual(bus.pendingMessageIds(), []);
  });
});

describe('应被阻止的发布：整次暂缓且状态零前进', () => {
  it('普通订阅者满员：发布暂缓，所有收件箱与共享组指针均不动', () => {
    const bus = new EventBus();
    bus.subscribe('slow', 'evt.#'); // 默认容量 1
    bus.createSharedGroup('g1', [{ id: 'm1' }, { id: 'm2' }]);
    bus.subscribeSharedGroup('g1', 'evt.#');

    const p1 = bus.publish('evt.1');
    assert.equal(p1.status, 'accepted');
    assert.equal(bus.cursorOf('g1'), 0); // m1 成功，指针从 -1 前进到 0
    assert.deepEqual(bus.memberInboxOf('g1', 'm1'), [p1.messageId]);

    // 第二次：slow 满员。即使共享组本可投给 m2，整次也必须暂缓。
    const p2 = bus.publish('evt.2');
    assert.equal(p2.status, 'deferred');
    assert.match(p2.reason, /slow 无空位/);
    assert.deepEqual(bus.pendingMessageIds(), [p2.messageId]);

    // 关键不变量：没有任何收件箱前进
    assert.deepEqual(bus.inboxOf('slow'), [p1.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm1'), [p1.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm2'), []);
    assert.equal(bus.cursorOf('g1'), 0); // 轮转指针未前进
  });

  it('共享组全部成员满员：命中的普通订阅者也不能收到该消息', () => {
    const bus = new EventBus();
    bus.subscribe('s1', 'evt.#');
    bus.createSharedGroup('g1', [{ id: 'm1' }]); // 单成员、容量 1
    bus.subscribeSharedGroup('g1', 'evt.#');

    const p1 = bus.publish('evt.1');
    assert.equal(p1.status, 'accepted');
    const p2 = bus.publish('evt.2'); // g1 无可用成员
    assert.equal(p2.status, 'deferred');
    assert.match(p2.reason, /g1 无可用成员/);

    // 原子性：普通订阅者同样没有收到第二次发布
    assert.deepEqual(bus.inboxOf('s1'), [p1.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm1'), [p1.messageId]);
    assert.deepEqual(bus.pendingMessageIds(), [p2.messageId]);
  });
});

describe('边界与交错：容量边界、暂缓后按原轮次投递、FIFO', () => {
  it('容量边界：恰好占满接受，超出一封即暂缓；释放后按原轮次投给共享组', () => {
    const bus = new EventBus();
    // slow 容量 2；共享组成员容量 2，保证暂缓时只有 slow 是瓶颈、组内仍有空位
    bus.subscribe('slow', 'evt.#', 2);
    bus.createSharedGroup('g1', [{ id: 'm1', capacity: 2 }, { id: 'm2', capacity: 2 }]);
    bus.subscribeSharedGroup('g1', 'evt.#');

    const p1 = bus.publish('evt.1');
    const p2 = bus.publish('evt.2');
    assert.equal(p1.status, 'accepted');
    assert.equal(p2.status, 'accepted'); // 恰好占满 slow 的两个空位
    assert.deepEqual(bus.inboxOf('slow'), [p1.messageId, p2.messageId]);
    // 共享组已轮转两次：m1、m2 各一封，指针在 m2
    assert.deepEqual(bus.memberInboxOf('g1', 'm1'), [p1.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm2'), [p2.messageId]);
    assert.equal(bus.cursorOf('g1'), 1);

    const p3 = bus.publish('evt.3'); // slow 满 -> 暂缓；共享组本可接单但整次必须一起等
    assert.equal(p3.status, 'deferred');
    assert.match(p3.reason, /slow 无空位/);
    assert.equal(bus.cursorOf('g1'), 1); // 暂缓期间指针不动
    assert.deepEqual(bus.pendingMessageIds(), [p3.messageId]);

    // 释放 slow 一封：p3 出队。共享组按“原本应得的轮次”从 m2 之后轮到 m1
    const release = bus.release('slow');
    assert.equal(release.released.id, p1.messageId);
    assert.equal(release.retry.processed, 1);
    assert.equal(release.retry.remaining, 0);
    assert.deepEqual(bus.pendingMessageIds(), []);
    assert.deepEqual(bus.inboxOf('slow'), [p2.messageId, p3.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm1'), [p1.messageId, p3.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm2'), [p2.messageId]);
    assert.equal(bus.cursorOf('g1'), 0); // 指针现在停在 m1
  });

  it('多个命中方同时满员时，只释放一方不足以解除暂缓（原子接受条件）', () => {
    const bus = new EventBus();
    bus.subscribe('slow', 'evt.#'); // 容量 1
    bus.createSharedGroup('g1', [{ id: 'm1' }]); // 单成员、容量 1
    bus.subscribeSharedGroup('g1', 'evt.#');

    const p1 = bus.publish('evt.1');
    assert.equal(p1.status, 'accepted');
    const p2 = bus.publish('evt.2');
    assert.equal(p2.status, 'deferred');
    assert.match(p2.reason, /slow 无空位/);
    assert.match(p2.reason, /g1 无可用成员/);

    // 只释放 slow：共享组仍满，p2 必须继续暂缓，任何状态都不前进
    let r = bus.release('slow');
    assert.equal(r.released.id, p1.messageId);
    assert.equal(r.retry.processed, 0);
    assert.equal(r.retry.remaining, 1);
    assert.deepEqual(bus.pendingMessageIds(), [p2.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm1'), [p1.messageId]);
    assert.equal(bus.cursorOf('g1'), 0);

    // 再释放共享组成员：所有命中方同时就绪，p2 才被接受
    r = bus.releaseGroupMember('g1', 'm1');
    assert.equal(r.released.id, p1.messageId);
    assert.equal(r.retry.processed, 1);
    assert.equal(r.retry.remaining, 0);
    assert.deepEqual(bus.inboxOf('slow'), [p2.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm1'), [p2.messageId]);
    assert.deepEqual(bus.pendingMessageIds(), []);
  });

  it('成员容量不均时跳过满员成员，但不改变“上次成功者之后轮转”的语义', () => {
    const bus = new EventBus();
    bus.createSharedGroup('g1', [
      { id: 'm1', capacity: 1 },
      { id: 'm2', capacity: 2 },
      { id: 'm3', capacity: 1 },
    ]);
    bus.subscribeSharedGroup('g1', 't.#');

    const p1 = bus.publish('t.1'); // 首次从首位 -> m1，cursor=0
    const p2 = bus.publish('t.2'); // 从 m1 之后 -> m2，cursor=1
    const p3 = bus.publish('t.3'); // 从 m2 之后 -> m3，cursor=2
    assert.equal(p3.status, 'accepted');
    assert.equal(bus.cursorOf('g1'), 2);

    // 下一轮从 m3 之后（m1）开始：m1 满 -> 跳过；m2 仍有空位 -> 投 m2
    const p4 = bus.publish('t.4');
    assert.equal(p4.status, 'accepted');
    assert.deepEqual(bus.memberInboxOf('g1', 'm1'), [p1.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm2'), [p2.messageId, p4.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm3'), [p3.messageId]);
    assert.equal(bus.cursorOf('g1'), 1); // 成功位置是 m2，而非扫描起点

    // 再下一轮从 m2 之后（m3）开始：m3 满、m1 满，m2 也恰好占满 -> 无人可选，暂缓
    const p5 = bus.publish('t.5');
    assert.equal(p5.status, 'deferred');
    assert.match(p5.reason, /无可用成员/);
    assert.equal(bus.cursorOf('g1'), 1); // 暂缓期间指针原地不动
    assert.deepEqual(bus.pendingMessageIds(), [p5.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm2'), [p2.messageId, p4.messageId]);

    // 释放 m3：重试仍从指针 m2 之后扫描，m2 满、m3 有空位 -> m3 收下
    const r = bus.releaseGroupMember('g1', 'm3');
    assert.equal(r.released.id, p3.messageId);
    assert.deepEqual(bus.memberInboxOf('g1', 'm3'), [p5.messageId]);
    assert.equal(bus.cursorOf('g1'), 2);
    assert.deepEqual(bus.pendingMessageIds(), []);
  });

  it('交错场景：普通订阅者暂缓不会消耗共享组轮次；多封暂缓严格 FIFO', () => {
    const bus = new EventBus();
    bus.subscribe('slow', 'evt.#'); // 容量 1，是唯一瓶颈
    // 共享组成员容量足够，保证暂缓期间组内始终有空位
    bus.createSharedGroup('g1', [{ id: 'm1', capacity: 8 }, { id: 'm2', capacity: 8 }]);
    bus.subscribeSharedGroup('g1', 'evt.#');

    const p1 = bus.publish('evt.1'); // 接受：slow 与共享组 m1 各收一份
    assert.equal(p1.status, 'accepted');
    const p2 = bus.publish('evt.2'); // 暂缓：slow 满
    const p3 = bus.publish('evt.3'); // 同样暂缓，排队在 p2 之后
    const p4 = bus.publish('evt.4'); // 继续排队，验证多封 FIFO
    assert.equal(p2.status, 'deferred');
    assert.equal(p3.status, 'deferred');
    assert.equal(p4.status, 'deferred');
    assert.deepEqual(bus.pendingMessageIds(), [p2.messageId, p3.messageId, p4.messageId]);
    // 共享组轮次未被暂缓消耗：仍停在 m1
    assert.equal(bus.cursorOf('g1'), 0);
    assert.deepEqual(bus.memberInboxOf('g1', 'm1'), [p1.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm2'), []);

    // 释放一次只腾出一个空位：p2 出队（共享组轮给 m2），p3、p4 仍受阻
    let r = bus.release('slow');
    assert.equal(r.released.id, p1.messageId);
    assert.equal(r.retry.processed, 1);
    assert.equal(r.retry.remaining, 2);
    assert.deepEqual(bus.pendingMessageIds(), [p3.messageId, p4.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm2'), [p2.messageId]);
    assert.equal(bus.cursorOf('g1'), 1);

    // 再次释放：p3 按 FIFO 出队，轮次从 m2 之后回到 m1；
    // slow 容量仅 1，p3 立即占满空位，队列里仍剩 p4 暂缓。
    r = bus.release('slow');
    assert.equal(r.released.id, p2.messageId);
    assert.equal(r.retry.processed, 1);
    assert.equal(r.retry.remaining, 1);
    assert.deepEqual(bus.memberInboxOf('g1', 'm1'), [p1.messageId, p3.messageId]);
    assert.deepEqual(bus.memberInboxOf('g1', 'm2'), [p2.messageId]);
    assert.equal(bus.cursorOf('g1'), 0);
    assert.deepEqual(bus.inboxOf('slow'), [p3.messageId]);
    assert.deepEqual(bus.pendingMessageIds(), [p4.messageId]);

    // 第三次释放：p4 出队，轮次从 m1 之后轮到 m2，队列清空
    r = bus.release('slow');
    assert.equal(r.released.id, p3.messageId);
    assert.equal(r.retry.remaining, 0);
    assert.deepEqual(bus.memberInboxOf('g1', 'm2'), [p2.messageId, p4.messageId]);
    assert.equal(bus.cursorOf('g1'), 1);
    assert.deepEqual(bus.inboxOf('slow'), [p4.messageId]);
  });

  it('不命中的发布不受满员影响，且不改变任何轮转状态', () => {
    const bus = new EventBus();
    bus.subscribe('s1', 'evt.#');
    bus.createSharedGroup('g1', [{ id: 'm1' }]);
    bus.subscribeSharedGroup('g1', 'evt.#');
    const p1 = bus.publish('evt.1');
    assert.equal(p1.status, 'accepted');

    const p2 = bus.publish('other.topic'); // 无任何命中方
    assert.equal(p2.status, 'accepted');
    assert.deepEqual(bus.pendingMessageIds(), []);
    assert.deepEqual(bus.inboxOf('s1'), [p1.messageId]);
    assert.equal(bus.cursorOf('g1'), 0);
  });
});
