import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { SessionOutput, SegmentStatus } from '../src/session-output.js';

const { PENDING, CONFIRMED, CORRECTED, STOPPED, INVALIDATED } = SegmentStatus;

// 收集会话发出的事件
function watch(session) {
  const events = [];
  for (const type of ['tokens', 'committed', 'corrected', 'invalidated', 'stopped']) {
    session.on(type, (entry) => events.push(entry));
  }
  return events;
}

function typesOf(events) {
  return events.map((e) => e.type);
}

describe('SessionOutput — 可接受的正常路径', () => {
  it('根段原样确认后提交，并可从提交头续链子段', () => {
    const s = new SessionOutput();
    const events = watch(s);

    s.submitSegment({ id: 'r', tokens: [11, 12, 13] });
    assert.equal(s.head, 0, '待验证的草稿不产生确定输出');
    assert.equal(s.getSegment('r').status, PENDING);

    s.verifySegment({ id: 'r', acceptedPrefixLength: 3 });
    assert.deepEqual(s.committedTokens, [11, 12, 13]);
    assert.equal(s.getSegment('r').status, CONFIRMED);
    assert.equal(s.activeRootId, null, '整条链落定后释放提交头');

    // 从已提交位置另起一条链
    s.submitSegment({ id: 'r2', tokens: [21] });
    assert.equal(s.getSegment('r2').start, 3);
    s.verifySegment({ id: 'r2', acceptedPrefixLength: 1 });
    assert.deepEqual(s.committedTokens, [11, 12, 13, 21]);
    assert.ok(events.some((e) => e.type === 'committed' && e.segmentId === 'r'));
  });

  it('子回复先到被缓冲，父段全数确认后子段才按序提交', () => {
    const s = new SessionOutput();
    // 父 [1,2,3]，子草稿从父草稿末尾（位置 3）开始
    s.submitSegment({ id: 'p', tokens: [1, 2, 3] });
    s.submitSegment({ id: 'c', parentId: 'p', tokens: [4, 5] });
    assert.equal(s.getSegment('c').start, 3);

    // 子回复先到：不能提交，只能缓冲
    s.verifySegment({ id: 'c', acceptedPrefixLength: 2 });
    assert.deepEqual(s.committedTokens, [], '父未确认，子回复先到不得提交');
    assert.equal(s.getSegment('c').status, PENDING);

    // 父段确认，泵出缓冲的子回复
    s.verifySegment({ id: 'p', acceptedPrefixLength: 3 });
    assert.deepEqual(s.committedTokens, [1, 2, 3, 4, 5]);
    assert.equal(s.getSegment('p').status, CONFIRMED);
    assert.equal(s.getSegment('c').status, CONFIRMED);
  });

  it('重连：按已收 Token 数只补余下的确定内容', () => {
    const s = new SessionOutput();
    s.submitSegment({ id: 'r', tokens: [7, 8, 9, 10] });
    s.verifySegment({ id: 'r', acceptedPrefixLength: 4 });

    assert.deepEqual(s.replay(0).tokens, [7, 8, 9, 10]);
    assert.deepEqual(s.replay(2).tokens, [9, 10]);
    const caught = s.replay(4);
    assert.deepEqual(caught.tokens, []);
    assert.equal(caught.caughtUp, true);
    assert.equal(caught.to, 4);
  });
});

describe('SessionOutput — 应被阻止/拒绝的结果', () => {
  it('活动链未落定时，不允许在提交头另起根段', () => {
    const s = new SessionOutput();
    s.submitSegment({ id: 'r', tokens: [1, 2] });
    assert.throws(
      () => s.submitSegment({ id: 'r2', tokens: [3] }),
      /活动链/,
    );
  });

  it('不能向已作废/已纠正的父段追加子段', () => {
    const s = new SessionOutput();
    s.submitSegment({ id: 'p', tokens: [1, 2, 3] });
    s.submitSegment({ id: 'c', parentId: 'p', tokens: [4] });
    s.verifySegment({ id: 'p', acceptedPrefixLength: 1, replacementToken: 9 });

    assert.equal(s.getSegment('c').status, INVALIDATED);
    assert.throws(() => s.submitSegment({ id: 'c2', parentId: 'c', tokens: [5] }), /作废/);
    assert.throws(() => s.submitSegment({ id: 'c3', parentId: 'p', tokens: [5] }), /纠正/);
  });

  it('同一父段不允许两条有效后继；重复 id 与非法输入被拒', () => {
    const s = new SessionOutput();
    s.submitSegment({ id: 'p', tokens: [1] });
    s.submitSegment({ id: 'c1', parentId: 'p', tokens: [2] });
    assert.throws(() => s.submitSegment({ id: 'c2', parentId: 'p', tokens: [3] }), /后继/);
    assert.throws(() => s.submitSegment({ id: 'p', tokens: [9] }), /重复/);
    assert.throws(() => s.submitSegment({ id: 'x', tokens: [] }), /非空/);
    assert.throws(() => s.submitSegment({ id: 'y', tokens: [1, 2.5] }), /整数/);
  });

  it('验证参数越界、全收却带替换项、重复验证均被拒绝', () => {
    const s = new SessionOutput();
    s.submitSegment({ id: 'r', tokens: [1, 2] });
    assert.throws(() => s.verifySegment({ id: 'r', acceptedPrefixLength: 3 }), /整数/);
    assert.throws(() => s.verifySegment({ id: 'r', acceptedPrefixLength: -1 }));
    assert.throws(
      () => s.verifySegment({ id: 'r', acceptedPrefixLength: 2, replacementToken: 5 }),
      /替换/,
    );
    s.verifySegment({ id: 'r', acceptedPrefixLength: 2 });
    assert.throws(() => s.verifySegment({ id: 'r', acceptedPrefixLength: 1 }), /重复验证/);
    assert.throws(() => s.verifySegment({ id: 'nope', acceptedPrefixLength: 0 }), /不存在/);
  });

  it('重连拒绝越界的已收计数', () => {
    const s = new SessionOutput();
    s.submitSegment({ id: 'r', tokens: [1] });
    s.verifySegment({ id: 'r', acceptedPrefixLength: 1 });
    assert.throws(() => s.replay(2), /超过/);
    assert.throws(() => s.replay(-1), /非负/);
  });

  it('终止后拒绝新草稿；作废子段的缓冲回复不再生效', () => {
    const s = new SessionOutput();
    s.submitSegment({ id: 'p', tokens: [1, 2] });
    s.submitSegment({ id: 'c', parentId: 'p', tokens: [3] });
    s.verifySegment({ id: 'c', acceptedPrefixLength: 1 }); // 缓冲
    s.verifySegment({ id: 'p', acceptedPrefixLength: 1, replacementToken: 0 });
    assert.equal(s.terminated, true);
    assert.equal(s.getSegment('c').status, INVALIDATED);
    assert.deepEqual(s.committedTokens, [1]);
    assert.throws(() => s.submitSegment({ id: 'r', tokens: [9] }), /终止/);
  });
});

describe('SessionOutput — 核心交错：整数 Token 重放「子回复先到、再遇父级纠正」', () => {
  it('父级纠正发出确认前缀与替换项，作废旧后继，并允许从纠正位置另起新链', () => {
    const s = new SessionOutput({ stopToken: 0 });
    const events = watch(s);

    // 提议链：p=[10,11,12] -> c=[13,14] -> gc=[15]
    s.submitSegment({ id: 'p', tokens: [10, 11, 12] });
    s.submitSegment({ id: 'c', parentId: 'p', tokens: [13, 14] });
    s.submitSegment({ id: 'gc', parentId: 'c', tokens: [15] });

    // 1) 子回复先到（孙、子），全部缓冲，确定输出仍为空
    s.verifySegment({ id: 'gc', acceptedPrefixLength: 1 });
    s.verifySegment({ id: 'c', acceptedPrefixLength: 2 });
    assert.deepEqual(s.committedTokens, []);
    assert.equal(s.getSegment('gc').status, PENDING);

    // 2) 父级被纠正：只收 2 个前缀，替换 Token 99
    s.verifySegment({ id: 'p', acceptedPrefixLength: 2, replacementToken: 99 });

    // 确认前缀 + 替换项已提交；旧后继全部作废
    assert.deepEqual(s.committedTokens, [10, 11, 99]);
    assert.equal(s.getSegment('p').status, CORRECTED);
    assert.equal(s.getSegment('p').replacement, 99);
    assert.equal(s.getSegment('c').status, INVALIDATED);
    assert.equal(s.getSegment('gc').status, INVALIDATED);
    assert.equal(s.activeRootId, null, '纠正后释放提交头，允许另起链');

    // 作废子段缓冲的回复被丢弃，不能再验证
    assert.equal(s.getSegment('c').bufferedResult, null);
    assert.throws(
      () => s.verifySegment({ id: 'c', acceptedPrefixLength: 1 }),
      /invalidated|作废/,
    );

    // 事件顺序：前缀/替换 tokens -> corrected -> invalidated
    const seq = typesOf(events);
    assert.deepEqual(seq, ['tokens', 'tokens', 'corrected', 'invalidated']);
    const invalidatedEvent = events.find((e) => e.type === 'invalidated');
    assert.deepEqual(invalidatedEvent.segmentIds, ['c', 'gc']);

    // 3) 从纠正后的已提交位置（head=3）另起一条链
    s.submitSegment({ id: 'p2', tokens: [20, 21] });
    assert.equal(s.getSegment('p2').start, 3);
    s.verifySegment({ id: 'p2', acceptedPrefixLength: 2 });
    assert.deepEqual(s.committedTokens, [10, 11, 99, 20, 21]);

    // 4) 重连补传：已收 2 个，只补余下确定内容（不含任何草稿/作废内容）
    const replay = s.replay(2);
    assert.deepEqual(replay.tokens, [99, 20, 21]);
    assert.equal(replay.from, 2);
    assert.equal(replay.to, 5);
  });

  it('截短且无替换项：只发确认前缀，后继作废，新链从该位置开始', () => {
    const s = new SessionOutput();
    s.submitSegment({ id: 'p', tokens: [1, 2, 3, 4] });
    s.submitSegment({ id: 'c', parentId: 'p', tokens: [5] });
    s.verifySegment({ id: 'c', acceptedPrefixLength: 1 }); // 子回复先到并缓冲
    s.verifySegment({ id: 'p', acceptedPrefixLength: 2 });  // 纯截短，无替换

    assert.deepEqual(s.committedTokens, [1, 2]);
    assert.equal(s.getSegment('p').status, CORRECTED);
    assert.equal(s.getSegment('p').replacement, null);
    assert.equal(s.getSegment('c').status, INVALIDATED);

    s.submitSegment({ id: 'p2', tokens: [8] });
    assert.equal(s.getSegment('p2').start, 2);
    s.verifySegment({ id: 'p2', acceptedPrefixLength: 1 });
    assert.deepEqual(s.committedTokens, [1, 2, 8]);
  });
});

describe('SessionOutput — 停止 Token 边界', () => {
  it('确认序列遇停止符终止，停止符及其后内容一律不发', () => {
    const s = new SessionOutput({ stopToken: 0 });
    s.submitSegment({ id: 'r', tokens: [1, 0, 2] });
    s.verifySegment({ id: 'r', acceptedPrefixLength: 3 });
    assert.deepEqual(s.committedTokens, [1]);
    assert.equal(s.getSegment('r').status, STOPPED);
    assert.equal(s.terminated, true);
  });

  it('停止符出现在接受前缀中：前缀截断，后继作废，会话终止', () => {
    const s = new SessionOutput({ stopToken: 0 });
    s.submitSegment({ id: 'p', tokens: [1, 0, 3] });
    s.submitSegment({ id: 'c', parentId: 'p', tokens: [4, 5] });
    s.verifySegment({ id: 'c', acceptedPrefixLength: 2 }); // 子回复先到
    s.verifySegment({ id: 'p', acceptedPrefixLength: 3 }); // 父段含停止符
    assert.deepEqual(s.committedTokens, [1]);
    assert.equal(s.getSegment('p').status, STOPPED);
    assert.equal(s.getSegment('c').status, INVALIDATED);
    assert.equal(s.terminated, true);
  });

  it('替换项本身是停止符：替换项不发，仅保留确认前缀并终止', () => {
    const s = new SessionOutput({ stopToken: 0 });
    s.submitSegment({ id: 'p', tokens: [1, 2, 3] });
    s.submitSegment({ id: 'c', parentId: 'p', tokens: [4] });
    s.verifySegment({ id: 'p', acceptedPrefixLength: 1, replacementToken: 0 });
    assert.deepEqual(s.committedTokens, [1]);
    assert.equal(s.terminated, true);
    assert.equal(s.getSegment('c').status, INVALIDATED);
  });

  it('停止符位于被拒绝的后缀中时不影响已接受前缀', () => {
    const s = new SessionOutput({ stopToken: 0 });
    // 接受前缀 [1,2]，停止符落在被拒绝的草稿后缀里
    s.submitSegment({ id: 'r', tokens: [1, 2, 0, 3] });
    s.verifySegment({ id: 'r', acceptedPrefixLength: 2, replacementToken: 7 });
    assert.deepEqual(s.committedTokens, [1, 2, 7]);
    assert.equal(s.getSegment('r').status, CORRECTED);
    assert.equal(s.terminated, false);
  });
});

describe('SessionOutput — 多级交错与事件', () => {
  it('三级链全部乱序回复，按链序一次性泵出', () => {
    const s = new SessionOutput();
    s.submitSegment({ id: 'a', tokens: [1] });
    s.submitSegment({ id: 'b', parentId: 'a', tokens: [2] });
    s.submitSegment({ id: 'c', parentId: 'b', tokens: [3] });

    // 回复顺序：孙 -> 子 -> 根
    s.verifySegment({ id: 'c', acceptedPrefixLength: 1 });
    s.verifySegment({ id: 'b', acceptedPrefixLength: 1 });
    assert.deepEqual(s.committedTokens, [], '根未确认前全部缓冲');
    s.verifySegment({ id: 'a', acceptedPrefixLength: 1 });
    assert.deepEqual(s.committedTokens, [1, 2, 3]);
    assert.equal(s.getSegment('c').status, CONFIRMED);
  });

  it('纠正发生在缓冲链中段：仅作废该段子树，已确认的祖先保留', () => {
    const s = new SessionOutput();
    s.submitSegment({ id: 'a', tokens: [1] });
    s.submitSegment({ id: 'b', parentId: 'a', tokens: [2, 3] });
    s.submitSegment({ id: 'c', parentId: 'b', tokens: [4] });

    s.verifySegment({ id: 'c', acceptedPrefixLength: 1 }); // 缓冲
    s.verifySegment({ id: 'a', acceptedPrefixLength: 1 }); // 根确认 -> [1]
    assert.deepEqual(s.committedTokens, [1]);

    // 中段 b 被纠正：c 作废，a 不受影响
    s.verifySegment({ id: 'b', acceptedPrefixLength: 1, replacementToken: 9 });
    assert.deepEqual(s.committedTokens, [1, 2, 9]);
    assert.equal(s.getSegment('a').status, CONFIRMED);
    assert.equal(s.getSegment('b').status, CORRECTED);
    assert.equal(s.getSegment('c').status, INVALIDATED);

    // 从 head=3 另起新链
    s.submitSegment({ id: 'd', tokens: [5] });
    assert.equal(s.getSegment('d').start, 3);
    s.verifySegment({ id: 'd', acceptedPrefixLength: 1 });
    assert.deepEqual(s.committedTokens, [1, 2, 9, 5]);
  });

  it('事件日志与提交内容一致，且包含 replacement 来源标记', () => {
    const s = new SessionOutput();
    const events = watch(s);
    s.submitSegment({ id: 'p', tokens: [1, 2, 3] });
    s.verifySegment({ id: 'p', acceptedPrefixLength: 2, replacementToken: 8 });

    const tokenEvents = events.filter((e) => e.type === 'tokens');
    assert.equal(tokenEvents[0].kind, 'segment');
    assert.deepEqual(tokenEvents[0].tokens, [1, 2]);
    assert.equal(tokenEvents[1].kind, 'replacement');
    assert.deepEqual(tokenEvents[1].tokens, [8]);
    assert.deepEqual(s.eventLog.length, events.length);
    assert.deepEqual(s.committedTokens, [1, 2, 8]);
  });
});
