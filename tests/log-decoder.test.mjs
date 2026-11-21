import { test } from 'node:test';
import assert from 'node:assert/strict';

import { LogDecoder, LogDecodeError } from '../src/log-decoder.mjs';

const enc = new TextEncoder();

function rec(overrides) {
  return {
    container: 'web',
    instance: 'i-1',
    stream: 'stdout',
    volume: 0,
    recordNo: 0,
    tag: 'F',
    bytes: enc.encode('line\n'),
    ...overrides,
  };
}

function concatBytes(...parts) {
  const total = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(total);
  let off = 0;
  for (const p of parts) {
    out.set(p, off);
    off += p.length;
  }
  return out;
}

test('可接受：跨卷拼行、UTF-8 字符被切开、stdout/stderr 交错', () => {
  const d = new LogDecoder();
  const zhong = enc.encode('中'); // E4 B8 AD，拆到两条片段里

  // 卷 0：stdout 半行开头
  assert.deepEqual(d.ingest(rec({ recordNo: 0, tag: 'P', bytes: enc.encode('hello ') })), []);
  // 卷 0：stderr 完整行交错进入，独立成行
  const errOut = d.ingest(rec({ recordNo: 1, stream: 'stderr', bytes: enc.encode('oops\n') }));
  assert.equal(errOut.length, 1);
  assert.equal(errOut[0].stream, 'stderr');
  assert.equal(errOut[0].text, 'oops');
  assert.deepEqual(errOut[0].start, { volume: 0, recordNo: 1 });
  assert.deepEqual(errOut[0].end, { volume: 0, recordNo: 1 });
  // 卷 0：stdout 半行续上「中」的前两个字节
  assert.deepEqual(
    d.ingest(rec({ recordNo: 2, tag: 'P', bytes: zhong.subarray(0, 2) })),
    [],
  );
  // 卷 1：stdout 收尾，含「中」的最后一个字节
  const out = d.ingest(
    rec({
      volume: 1,
      recordNo: 0,
      tag: 'F',
      bytes: concatBytes(zhong.subarray(2), enc.encode(' world\n')),
    }),
  );
  assert.equal(out.length, 1);
  assert.equal(out[0].text, 'hello 中 world');
  assert.equal(out[0].partial, false);
  // 下游可查到该行覆盖的首末记录位置（跨卷）
  assert.deepEqual(out[0].start, { volume: 0, recordNo: 0 });
  assert.deepEqual(out[0].end, { volume: 1, recordNo: 0 });
});

test('应被拒绝：位置跳跃与非法字段', () => {
  const d = new LogDecoder();
  d.ingest(rec({ recordNo: 0, bytes: enc.encode('a\n') }));

  // 同卷记录号跳跃：拒绝
  assert.throws(
    () => d.ingest(rec({ recordNo: 2, bytes: enc.encode('b\n') })),
    (err) => err instanceof LogDecodeError && err.code === 'ERR_POSITION_GAP',
  );
  // 卷序号跳跃：拒绝
  assert.throws(
    () => d.ingest(rec({ volume: 2, recordNo: 0, bytes: enc.encode('b\n') })),
    (err) => err instanceof LogDecodeError && err.code === 'ERR_POSITION_GAP',
  );
  // 非法标记：拒绝
  assert.throws(
    () => d.ingest(rec({ recordNo: 1, tag: 'X' })),
    (err) => err instanceof LogDecodeError && err.code === 'ERR_BAD_TAG',
  );
  // 非法流名：拒绝
  assert.throws(
    () => d.ingest(rec({ recordNo: 1, stream: 'stdall' })),
    (err) => err instanceof LogDecodeError && err.code === 'ERR_BAD_STREAM',
  );
  // 被拒绝的记录不污染进度：合法后继仍可被接受
  const out = d.ingest(rec({ recordNo: 1, bytes: enc.encode('b\n') }));
  assert.equal(out.length, 1);
  assert.equal(out[0].text, 'b');
});

test('边界交错：快照恢复后重送去重，半行只补完产出一次', () => {
  const before = new LogDecoder();
  // 半行进入后保存进度（JSON 往返模拟落盘再打开）
  assert.deepEqual(before.ingest(rec({ recordNo: 0, tag: 'P', bytes: enc.encode('abc') })), []);
  const snapshot = JSON.parse(JSON.stringify(before.snapshot()));

  const after = LogDecoder.restore(snapshot);
  // 上游重送已保存的记录：静默去重，不产出、不报错
  assert.deepEqual(after.ingest(rec({ recordNo: 0, tag: 'P', bytes: enc.encode('abc') })), []);
  // 补完半行：与重送片段拼成一行，只产出一次
  const out = after.ingest(rec({ recordNo: 1, tag: 'F', bytes: enc.encode('def\n') }));
  assert.equal(out.length, 1);
  assert.equal(out[0].text, 'abcdef');
  assert.deepEqual(out[0].start, { volume: 0, recordNo: 0 });
  assert.deepEqual(out[0].end, { volume: 0, recordNo: 1 });
  // 连收尾记录也被重送：不会二次产出
  assert.deepEqual(after.ingest(rec({ recordNo: 1, tag: 'F', bytes: enc.encode('def\n') })), []);
  // 恢复后再次快照仍可继续
  const again = LogDecoder.restore(JSON.parse(JSON.stringify(after.snapshot())));
  const more = again.ingest(rec({ recordNo: 2, bytes: enc.encode('next\n') }));
  assert.equal(more.length, 1);
  assert.equal(more[0].text, 'next');
});

test('实例重启：旧实例半行先作为残缺记录输出，再处理新实例首行', () => {
  const d = new LogDecoder();
  // 旧实例留下半行（无 F 收尾）
  assert.deepEqual(d.ingest(rec({ recordNo: 0, tag: 'P', bytes: enc.encode('old-half') })), []);

  // 同一容器换了实例：一次摄入带回两条产出
  const out = d.ingest(
    rec({ instance: 'i-2', recordNo: 0, bytes: enc.encode('new-first\n') }),
  );
  assert.equal(out.length, 2);

  // 先冲刷旧实例的残缺记录
  assert.equal(out[0].instance, 'i-1');
  assert.equal(out[0].partial, true);
  assert.equal(out[0].text, 'old-half');
  assert.deepEqual(out[0].start, { volume: 0, recordNo: 0 });
  assert.deepEqual(out[0].end, { volume: 0, recordNo: 0 });

  // 再产出新实例首行，位置序列从新实例重新计数
  assert.equal(out[1].instance, 'i-2');
  assert.equal(out[1].partial, false);
  assert.equal(out[1].text, 'new-first');
  assert.deepEqual(out[1].start, { volume: 0, recordNo: 0 });

  // 新实例后续记录按自身序列推进
  const more = d.ingest(rec({ instance: 'i-2', recordNo: 1, bytes: enc.encode('second\n') }));
  assert.equal(more.length, 1);
  assert.equal(more[0].text, 'second');
});

test('停止时 flushAll 冲刷所有未完成半行', () => {
  const d = new LogDecoder();
  d.ingest(rec({ recordNo: 0, tag: 'P', bytes: enc.encode('tail-') }));
  d.ingest(rec({ recordNo: 1, stream: 'stderr', tag: 'P', bytes: enc.encode('err-half') }));

  const flushed = d.flushAll();
  assert.equal(flushed.length, 2);
  const byStream = Object.fromEntries(flushed.map((l) => [l.stream, l]));
  assert.equal(byStream.stdout.text, 'tail-');
  assert.equal(byStream.stdout.partial, true);
  assert.deepEqual(byStream.stdout.start, { volume: 0, recordNo: 0 });
  assert.deepEqual(byStream.stdout.end, { volume: 0, recordNo: 0 });
  assert.equal(byStream.stderr.text, 'err-half');
  assert.deepEqual(byStream.stderr.start, { volume: 0, recordNo: 1 });
});
