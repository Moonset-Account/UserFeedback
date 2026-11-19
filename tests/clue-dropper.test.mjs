import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ClueDropper, ClueDropperError } from '../src/clue-dropper.mjs';

const names = (cards) => cards.map((c) => c.name);

/** 构造一套标准演练卡面。 */
function buildExercise() {
  const ex = new ClueDropper();
  ex.addCard({ id: 'C1', name: '外网告警', seats: ['red', 'blue'] });
  ex.addCard({
    id: 'C2',
    name: '主机隔离步骤',
    seats: ['red'],
    requiresDecisions: [{ decision: 'isolation', option: 'host' }],
  });
  ex.addCard({
    id: 'C3',
    name: '网络隔离步骤',
    seats: ['blue'],
    requiresDecisions: [{ decision: 'isolation', option: 'network' }],
  });
  ex.addCard({
    id: 'C4',
    name: '后续情报',
    seats: ['red', 'blue'],
    requiresAck: { group: 'blue', card: 'C1' },
  });
  ex.addCard({
    id: 'C5',
    name: '红队复核结论',
    seats: ['blue'],
    requiresAck: { group: 'red', card: 'C1' },
  });
  return ex;
}

test('可接受结果：决定作出后，声明席位看到解锁的卡，未声明席位看不到', () => {
  const ex = buildExercise();
  ex.createBranch('main');
  ex.drop('main', 'C1');
  ex.drop('main', 'C2');
  ex.decide('main', 'isolation', 'host', { by: 'red' });

  assert.deepEqual(names(ex.visibleCards('main', 'red')), ['外网告警', '主机隔离步骤']);
  // C2 未投放到 blue 席位，blue 只能看到 C1
  assert.deepEqual(names(ex.visibleCards('main', 'blue')), ['外网告警']);
});

test('应被阻止或拒绝：未解锁卡名不泄露，伪造阅知与非法操作被拒绝', () => {
  const ex = buildExercise();
  ex.createBranch('main');
  ex.drop('main', 'C1');
  ex.drop('main', 'C2');
  ex.drop('main', 'C3');
  ex.drop('main', 'C4');

  // isolation 决定尚未作出：C2/C3 锁定；blue 尚未确认 C1：C4 锁定。
  // 两个席位查询结果中都不得出现这些卡名。
  assert.deepEqual(names(ex.visibleCards('main', 'red')), ['外网告警']);
  assert.deepEqual(names(ex.visibleCards('main', 'blue')), ['外网告警']);
  const leaked = [...ex.visibleCards('main', 'red'), ...ex.visibleCards('main', 'blue')]
    .map((c) => c.name);
  for (const hidden of ['主机隔离步骤', '网络隔离步骤', '后续情报']) {
    assert.ok(!leaked.includes(hidden), `locked card name leaked: ${hidden}`);
  }

  // 伪造阅知：red 组并未在 C2 的可见席位中，确认应被拒绝
  assert.throws(() => ex.ack('main', 'red', 'C2'), (err) => {
    assert.ok(err instanceof ClueDropperError);
    assert.equal(err.code, 'ACK_REJECTED');
    return true;
  });
  // 对尚未解锁（前置阅知未满足）的卡做确认，同样被拒绝
  assert.throws(() => ex.ack('main', 'red', 'C4'), { code: 'ACK_REJECTED' });

  // 非法操作：未知卡投放、未知分支、重复决定
  assert.throws(() => ex.drop('main', 'NOPE'), { code: 'UNKNOWN_CARD' });
  assert.throws(() => ex.drop('ghost', 'C1'), { code: 'UNKNOWN_BRANCH' });
  ex.decide('main', 'isolation', 'host', { by: 'red' });
  assert.throws(() => ex.decide('main', 'isolation', 'network', { by: 'blue' }),
    { code: 'DECISION_ALREADY_MADE' });
});

test('边界与交错：决定前分叉，两分支作不同隔离决定，两席位各自重放', () => {
  const ex = buildExercise();
  ex.createBranch('main');
  ex.drop('main', 'C1');
  ex.drop('main', 'C2');
  ex.drop('main', 'C3');
  ex.drop('main', 'C4');
  ex.drop('main', 'C5');
  ex.ack('main', 'blue', 'C1'); // 分叉前：blue 已确认看过 C1
  ex.decide('main', 'isolation', 'host', { by: 'red' }); // 主线：主机隔离

  // 导演从 isolation 决定之前另开分支：继承投放与 blue 的阅知，不含该决定
  ex.fork('main', 'alt', { beforeDecision: 'isolation' });
  ex.decide('alt', 'isolation', 'network', { by: 'blue' }); // 分支：网络隔离

  // 分叉后主线独立推进：red 在主线确认 C1（解锁 C5），分支不受影响
  ex.ack('main', 'red', 'C1');

  // --- 主线重放 ---
  // red：C1(投放) → C4(blue 阅知解锁) → C2(host 决定解锁)；C3 不属于 red 席位
  assert.deepEqual(ex.replay('main', 'red').map((c) => c.id), ['C1', 'C4', 'C2']);
  // blue：C1 → C4 → C5(red 在主线确认了 C1)；C3 需 network 决定，主线未解锁且不得泄露
  assert.deepEqual(ex.replay('main', 'blue').map((c) => c.id), ['C1', 'C4', 'C5']);
  assert.ok(!names(ex.visibleCards('main', 'blue')).includes('网络隔离步骤'));

  // --- 分支重放 ---
  // blue：继承的 C1、C4 仍在；network 决定解锁 C3；C5 因 red 的阅知只发生在主线而锁定
  assert.deepEqual(ex.replay('alt', 'blue').map((c) => c.id), ['C1', 'C4', 'C3']);
  assert.ok(!names(ex.visibleCards('alt', 'blue')).includes('红队复核结论'));
  // red：host 决定只发生在主线，C2 在分支锁定且卡名不得出现
  assert.deepEqual(ex.replay('alt', 'red').map((c) => c.id), ['C1', 'C4']);
  assert.ok(!names(ex.visibleCards('alt', 'red')).includes('主机隔离步骤'));

  // 两条分支互不影响：各自真正掌握的线索不同
  assert.notDeepEqual(ex.replay('main', 'blue').map((c) => c.id),
    ex.replay('alt', 'blue').map((c) => c.id));
});

test('边界：分叉点校验与阅知门槛的临界时刻', () => {
  const ex = buildExercise();
  ex.createBranch('main');
  ex.drop('main', 'C1');
  ex.drop('main', 'C4');

  // 从不存在的决定分叉应被拒绝
  assert.throws(() => ex.fork('main', 'alt', { beforeDecision: 'isolation' }),
    { code: 'DECISION_NOT_FOUND' });

  // 临界时刻：blue 确认 C1 之前，C4 锁定；确认之后立即解锁
  assert.deepEqual(names(ex.visibleCards('main', 'red')), ['外网告警']);
  ex.ack('main', 'blue', 'C1');
  assert.deepEqual(names(ex.visibleCards('main', 'red')), ['外网告警', '后续情报']);
  // 重复确认被拒绝
  assert.throws(() => ex.ack('main', 'blue', 'C1'), { code: 'DUPLICATE_ACK' });
});
