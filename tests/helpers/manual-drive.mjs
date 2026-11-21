// 手动驱动示例（非自动测试文件，命名不匹配 *.test.mjs）。
//
// 用法：node tests/helpers/manual-drive.mjs
// 用最小脚本演示一次“客户端先半关闭、服务端后回响应”的完整中继，
// 并打印事件次序与各方向缓冲峰值，便于人工观察。

import { DuplexRelay } from '../../src/duplex-relay.js';
import { ControllableDuplex, EventClock } from './controllable-duplex.js';

function makeBytes(start, length) {
  return Buffer.from(Array.from({ length }, (_, i) => (start + i) & 0xff));
}

const clock = new EventClock();
const a = new ControllableDuplex('a', { readStep: 3, writeStep: 2, sendCapacity: 8, clock });
const b = new ControllableDuplex('b', { readStep: 5, writeStep: 4, sendCapacity: 12, clock });
const relay = new DuplexRelay(a, b, { aToBLimit: 7, bToALimit: 9 });
const done = relay.run();

a.feed(makeBytes(0, 30));
a.feedEnd();
for (let i = 0; i < 100 && !b.writeEnded; i += 1) b.flush();

b.feed(makeBytes(100, 40));
b.feedEnd();
for (let i = 0; i < 100 && !a.writeEnded; i += 1) a.flush();

const report = await done;
console.log('clean =', report.clean);
console.log('aToB peak =', report.directions.aToB.peakBufferedBytes,
  'written =', report.directions.aToB.bytesWritten);
console.log('bToA peak =', report.directions.bToA.peakBufferedBytes,
  'written =', report.directions.bToA.bytesWritten);
console.log('writeEnd order =', clock.eventsOf('writeEnd').map((e) => `${e.stream}#${e.seq}`));
console.log('b content matches request =', b.receivedContent().equals(makeBytes(0, 30)));
console.log('a content matches response =', a.receivedContent().equals(makeBytes(100, 40)));
