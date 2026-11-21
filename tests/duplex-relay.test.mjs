// 中继验收测试（node:test + 本地合成流，无网络、无真实负载）。
//
// 覆盖题面交叉约束：
//   1. 可接受结果：双向转发字节逐字节一致、半关闭排空后才传递 end、写结束次序、
//      部分写入与 drain、各方向缓冲峰值不越界。
//   2. 应被阻止/拒绝：恢复写入时硬断开 -> 中继终止，上报两边未写字节，拆除监听。
//   3. 边界/交错：readStep=1 时峰值精确等于上限、半关闭遇阻塞须等待、
//      一个方向背压不影响另一方向、部分接受的记账守恒。

import test from 'node:test';
import assert from 'node:assert/strict';
import { DuplexRelay } from '../src/duplex-relay.js';
import { ControllableDuplex, EventClock } from './helpers/controllable-duplex.js';

function makeBytes(start, length) {
  return Buffer.from(Array.from({ length }, (_, i) => (start + i) & 0xff));
}

/** 反复 flush 直到条件成立；条件不可能成立时显式失败而不是死循环。 */
function flushUntil(stream, predicate, maxTicks = 1000) {
  let ticks = 0;
  while (!predicate()) {
    if (ticks++ >= maxTicks) throw new Error(`flushUntil 超时: ${stream.name}`);
    stream.flush();
  }
}

function makePair(clock, optsA, optsB) {
  const a = new ControllableDuplex('a', { ...optsA, clock });
  const b = new ControllableDuplex('b', { ...optsB, clock });
  return { a, b };
}

test('可接受结果：客户端先半关闭，服务端稍后回大段响应；字节、次序、峰值均正确', async () => {
  const clock = new EventClock();
  const { a, b } = makePair(
    clock,
    { readStep: 3, writeStep: 2, sendCapacity: 8 },
    { readStep: 5, writeStep: 4, sendCapacity: 12 },
  );
  const relay = new DuplexRelay(a, b, { aToBLimit: 7, bToALimit: 9 });
  const done = relay.run();

  // 客户端发请求并立即关闭写端（题面场景）。
  const request = makeBytes(0, 30);
  a.feed(request);
  a.feedEnd();
  // 目的端 b 发送缓冲会阻塞，反复 flush 直到 a->b 方向干净结束。
  flushUntil(b, () => b.writeEnded);

  // 服务端“稍后”才回一大段响应，再关自己的写端。
  assert.equal(relay.aToB.stats().done, true);
  const response = makeBytes(100, 40);
  b.feed(response);
  b.feedEnd();
  flushUntil(a, () => a.writeEnded);

  const report = await done;

  // 结算：干净完成，两边均无未写字节。
  assert.equal(report.clean, true);
  assert.equal(report.failure, null);
  assert.equal(report.directions.aToB.unwrittenAtEnd, 0);
  assert.equal(report.directions.bToA.unwrittenAtEnd, 0);

  // 逐字节核对两端收到的内容。
  assert.deepEqual(b.receivedContent(), request);
  assert.deepEqual(a.receivedContent(), response);

  // 写结束次序：b 收到 a 的写结束，必须早于 a 收到 b 的写结束。
  const endA = clock.eventsOf('writeEnd').find((e) => e.stream === 'a').seq;
  const endB = clock.eventsOf('writeEnd').find((e) => e.stream === 'b').seq;
  assert.ok(endB < endA, '客户端写结束应先于服务端响应的写结束');

  // 任一端的 writeEnd 都不得早于该端最后一次接受字节。
  for (const stream of [a, b]) {
    const lastAccept = Math.max(
      0,
      ...clock.eventsOf('writeAccept').filter((e) => e.stream === stream.name).map((e) => e.seq),
    );
    const endSeq = clock.eventsOf('writeEnd').find((e) => e.stream === stream.name).seq;
    assert.ok(endSeq > lastAccept, `${stream.name} 的写结束必须在最后一个字节被接受之后`);
  }

  // 字节守恒。
  assert.equal(report.directions.aToB.bytesRead, 30);
  assert.equal(report.directions.aToB.bytesWritten, 30);
  assert.equal(report.directions.bToA.bytesRead, 40);
  assert.equal(report.directions.bToA.bytesWritten, 40);

  // 峰值上限：单次读取步长可能造成不超过 readStep-1 的过冲，不得再高。
  assert.ok(report.directions.aToB.peakBufferedBytes >= 1);
  assert.ok(
    report.directions.aToB.peakBufferedBytes <= 7 + 3 - 1,
    'aToB 峰值越界',
  );
  assert.ok(report.directions.bToA.peakBufferedBytes >= 1);
  assert.ok(
    report.directions.bToA.peakBufferedBytes <= 9 + 5 - 1,
    'bToA 峰值越界',
  );

  // 过程中确实发生过部分接受与阻塞恢复，而不是一路直通。
  assert.ok(clock.eventsOf('writeBlocked').length >= 1);
  assert.ok(clock.eventsOf('drain').length >= 1);
});

test('背压隔离：目的端阻塞只暂停对应源端，另一方向仍持续转发', async () => {
  const clock = new EventClock();
  const { a, b } = makePair(
    clock,
    { readStep: 4, writeStep: 5, sendCapacity: 64 }, // a 作为 bToA 目的端：永不阻塞
    { readStep: 3, writeStep: 2, sendCapacity: 6 },  // b 作为 aToB 目的端：很快阻塞
  );
  const relay = new DuplexRelay(a, b, { aToBLimit: 8, bToALimit: 64 });
  const done = relay.run();

  // a -> b 方向制造阻塞，不 flush。
  a.feed(makeBytes(0, 24));
  assert.equal(relay.aToB.stats().writeBlocked, true);
  assert.equal(relay.aToB.stats().paused, true);
  assert.ok(relay.aToB.stats().peakBufferedBytes >= 8);

  // 同时 b -> a 方向必须不受影响，全部直达。
  const otherPayload = makeBytes(50, 18);
  b.feed(otherPayload);
  assert.deepEqual(a.receivedContent(), otherPayload);
  assert.equal(relay.bToA.stats().writeBlocked, false);
  assert.equal(relay.bToA.stats().paused, false);
  assert.equal(relay.bToA.stats().bufferedBytes, 0);

  // 暂停事件只应出现在源端 a，b 从未被暂停。
  assert.ok(clock.eventsOf('pause').some((e) => e.stream === 'a'));
  assert.equal(clock.eventsOf('pause').filter((e) => e.stream === 'b').length, 0);

  // 恢复 aToB：排空后半关闭，完整到达。
  a.feedEnd();
  flushUntil(b, () => b.writeEnded);
  b.feedEnd();
  flushUntil(a, () => a.writeEnded);

  const report = await done;
  assert.equal(report.clean, true);
  assert.deepEqual(b.receivedContent(), makeBytes(0, 24));
  assert.equal(report.directions.aToB.unwrittenAtEnd, 0);
  assert.equal(report.directions.bToA.unwrittenAtEnd, 0);
});

test('硬断开（应拒绝）：正要恢复写入时报错，中继终止并上报两边未写字节', async () => {
  const clock = new EventClock();
  const { a, b } = makePair(
    clock,
    { readStep: 2, writeStep: 2, sendCapacity: 5 },
    { readStep: 2, writeStep: 3, sendCapacity: 5 },
  );
  const relay = new DuplexRelay(a, b, { aToBLimit: 64, bToALimit: 64 });
  let terminateReport = null;
  relay.on('terminate', (r) => { terminateReport = r; });
  const done = relay.run();

  // 两个方向都制造“目的端缓冲满 + 中继仍有排队字节”的状态。
  a.feed(makeBytes(0, 30)); // b 接受 5，aToB 排队 25
  b.feed(makeBytes(80, 10)); // a 接受 5，bToA 排队 5
  assert.equal(relay.aToB.stats().writeBlocked, true);
  assert.equal(relay.bToA.stats().writeBlocked, true);

  // 在 b 正要恢复写入（drain 时刻）报告硬断开。
  b.failOnNextWake('ECONNRESET');
  b.flush();

  const report = await done;
  assert.equal(report.clean, false);
  assert.equal(report.failure.direction, 'aToB');
  assert.match(report.failure.message, /ECONNRESET/);
  assert.equal(terminateReport, report, 'terminate 事件应携带同一份结算报告');

  // 未写字节上报 + 字节守恒（已被目的端接受的字节不计入未写）。
  const ab = report.directions.aToB;
  const ba = report.directions.bToA;
  assert.equal(ab.unwrittenAtEnd, 25);
  assert.equal(ab.bytesWritten + ab.unwrittenAtEnd, ab.bytesRead);
  assert.equal(ba.unwrittenAtEnd, 5);
  assert.equal(ba.bytesWritten + ba.unwrittenAtEnd, ba.bytesRead);

  // 监听全部拆除，终止后再喂数据不会被转发。
  for (const s of [a, b]) {
    assert.equal(s.listenerCount('data'), 0);
    assert.equal(s.listenerCount('end'), 0);
    assert.equal(s.listenerCount('drain'), 0);
    assert.equal(s.listenerCount('error'), 0);
  }
  const deliveredBefore = b.delivered.length;
  a.feed(makeBytes(200, 8));
  assert.equal(b.delivered.length, deliveredBefore);
});

test('硬断开边界：无排队字节时立即报错，未写计数为 0，中继仍以失败结算', async () => {
  const clock = new EventClock();
  const { a, b } = makePair(clock, {}, {});
  const relay = new DuplexRelay(a, b);
  const done = relay.run();

  a.triggerHardError('EPIPE');
  const report = await done;
  assert.equal(report.clean, false);
  assert.match(report.failure.message, /EPIPE/);
  assert.equal(report.directions.aToB.unwrittenAtEnd, 0);
  assert.equal(report.directions.bToA.unwrittenAtEnd, 0);
});

test('边界：readStep=1 时峰值精确等于上限；半关闭遇阻塞须等排空后才传递', async () => {
  const clock = new EventClock();
  const { a, b } = makePair(
    clock,
    { readStep: 1, writeStep: 2, sendCapacity: 4 },
    { readStep: 2, writeStep: 1, sendCapacity: 3 },
  );
  const relay = new DuplexRelay(a, b, { aToBLimit: 4, bToALimit: 16 });
  const done = relay.run();

  const payload = makeBytes(10, 10);
  a.feed(payload);
  a.feedEnd();

  // 阻塞期间：源端在缓冲打满时已被暂停，end 标记还滞留在读队列里；
  // 无论 end 标记是否已被中继消费，写结束都绝不能提前传给 b。
  assert.equal(b.writeEnded, false);
  assert.equal(relay.aToB.stats().peakBufferedBytes, 4, '逐字节读取下峰值应精确等于上限');
  assert.equal(relay.aToB.stats().writeBlocked, true);
  if (relay.aToB.stats().sourceEnded) {
    // 即便 end 已到中继，也必须等字节排空，未写字节不得为 0 之外的“已完成”态。
    assert.equal(relay.aToB.stats().done, false);
  } else {
    assert.ok(a.readQueuedBytes >= 1, 'end 标记应仍在暂停的读队列中等待');
  }

  // 交错方向：b 同时回数据，独立缓冲、独立峰值。
  const reply = makeBytes(60, 6);
  b.feed(reply);
  b.feedEnd();

  flushUntil(b, () => b.writeEnded);
  const lastDrainOnB = Math.max(...clock.eventsOf('drain').filter((e) => e.stream === 'b').map((e) => e.seq));
  const endSeqOnB = clock.eventsOf('writeEnd').find((e) => e.stream === 'b').seq;
  assert.ok(endSeqOnB > lastDrainOnB, '写结束必须在最后一次 drain 排空之后');

  flushUntil(a, () => a.writeEnded);
  const report = await done;

  assert.equal(report.clean, true);
  assert.deepEqual(b.receivedContent(), payload);
  assert.deepEqual(a.receivedContent(), reply);
  assert.equal(report.directions.aToB.peakBufferedBytes, 4);
  assert.ok(report.directions.bToA.peakBufferedBytes >= 1);
  assert.equal(report.directions.aToB.unwrittenAtEnd, 0);
});

test('部分写入记账：write 一次只接受部分字节，内容不丢不重，守恒成立', async () => {
  const clock = new EventClock();
  const { a, b } = makePair(
    clock,
    { readStep: 6, writeStep: 3, sendCapacity: 32 },
    { readStep: 4, writeStep: 2, sendCapacity: 7 }, // 2 < 读块 6，必然出现部分接受
  );
  const relay = new DuplexRelay(a, b, { aToBLimit: 64, bToALimit: 64 });
  const done = relay.run();

  const payload = makeBytes(0, 25);
  a.feed(payload);
  a.feedEnd();
  b.feedEnd();
  flushUntil(b, () => b.writeEnded);

  const report = await done;
  assert.equal(report.clean, true);
  assert.deepEqual(b.receivedContent(), payload);
  assert.equal(report.directions.aToB.bytesWritten, 25);
  assert.equal(report.directions.aToB.unwrittenAtEnd, 0);

  // 确实观测到“接受数 < 请求数”的部分写入。
  assert.ok(
    clock.eventsOf('writeAccept').some((e) => e.stream === 'b' && e.detail.accept < e.detail.wanted),
  );
});

test('防御：目的端 write 返回非法值或同步抛错时，中继以失败结算而非失控', async () => {
  const clock = new EventClock();
  const a = new ControllableDuplex('a', { clock });
  const fakeB = new ControllableDuplex('b', { clock });
  fakeB.write = () => 999; // 超出 chunk 长度的非法返回
  const relay = new DuplexRelay(a, fakeB);
  const done = relay.run();
  a.feed(Buffer.from([1, 2, 3]));
  const report = await done;
  assert.equal(report.clean, false);
  assert.match(report.failure.message, /非法的 write 返回值/);

  // 同步抛错路径。
  const clock2 = new EventClock();
  const a2 = new ControllableDuplex('a', { clock: clock2 });
  const b2 = new ControllableDuplex('b', { clock: clock2 });
  b2.write = () => { throw new Error('boom'); };
  const relay2 = new DuplexRelay(a2, b2);
  const done2 = relay2.run();
  a2.feed(Buffer.from([9]));
  const report2 = await done2;
  assert.equal(report2.clean, false);
  assert.match(report2.failure.message, /boom/);
});
