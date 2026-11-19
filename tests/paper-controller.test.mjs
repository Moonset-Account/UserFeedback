import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';

import {
  PaperOutputController,
  SheetStatus,
  Side,
  BackPolicy,
  ErrorCode,
  PaperControllerError,
  OutputSensor,
  RecordingGate,
  paperRegistered,
  sideImaged,
  paperOutput,
  pickupAuthorized,
  paperJammed,
  paperRecycled,
  replay,
} from '../src/index.js';

/**
 * 验收用例（node:test，真实断言）。
 *
 * 交叉约束覆盖矩阵：
 *  A. 可接受结果：双面正常开格、单面 BLANK 开格、乱序证据齐备后开格、
 *     卡纸→隔离→新纸交付→旧纸回收的完整闭环。
 *  B. 应被阻止：缺任一证据不开格、他人授权拒绝、跨纸半面回执拒绝、
 *     隔离旧纸补事件拒绝、未在册传感器拒绝、单面纸伪造背面拒绝、
 *     未声明空白背面登记拒绝。
 *  C. 边界/交错：重复与乱序重放不重复开格、事件日志二次重放状态一致、
 *     旧纸半面回执撞新纸号、闸门故障重试、运行中替换传感器与闸门。
 */

const ALICE = 'user-alice';
const BOB = 'user-bob';

function makeController({ gate, sensor, t = 1000 } = {}) {
  let now = t;
  const clock = () => {
    now += 1;
    return now;
  };
  const controller = new PaperOutputController({ gate, sensor, clock });
  return controller;
}

/** 双面纸 P1（Alice）登记。 */
function regDuplex(controller, paperId = 'P-001', owner = ALICE, job = 'J-1', box = 'BOX-A') {
  return controller.handle(
    paperRegistered({ paperId, ownerId: owner, jobId: job, compartmentId: box, backPolicy: BackPolicy.PRINTED },
      `reg-${paperId}`),
  );
}

/** 单面纸登记（显式 BLANK 背面）。 */
function regSimplex(controller, paperId, owner, job, box) {
  return controller.handle(
    paperRegistered({ paperId, ownerId: owner, jobId: job, compartmentId: box, backPolicy: BackPolicy.BLANK },
      `reg-${paperId}`),
  );
}

describe('保密出纸控制器', () => {
  let gate;
  let sensor;
  let controller;

  beforeEach(() => {
    gate = new RecordingGate();
    sensor = new OutputSensor('SENSOR-1');
    controller = makeController({ gate, sensor });
  });

  // ----------------------------------------------------- A. 可接受结果

  test('A1: 双面任务两面成像 + 出纸 + 同一人授权，证据核到同一纸后恰好开格一次', () => {
    regDuplex(controller);
    const r1 = controller.handle(sideImaged({ paperId: 'P-001', ownerId: ALICE, jobId: 'J-1', side: Side.FRONT }, 'img-f'));
    assert.equal(r1.status, SheetStatus.REGISTERED);
    assert.equal(r1.release.opened, false);
    assert.equal(r1.release.reason, 'EVIDENCE_INCOMPLETE');

    const r2 = controller.handle(paperOutput({ paperId: 'P-001', sensorId: 'SENSOR-1' }, 'out-1'));
    assert.equal(r2.release.opened, false); // 背面与授权未齐

    controller.handle(sideImaged({ paperId: 'P-001', ownerId: ALICE, jobId: 'J-1', side: Side.BACK }, 'img-b'));
    const final = controller.handle(pickupAuthorized({ paperId: 'P-001', ownerId: ALICE }, 'auth-1'));

    assert.equal(final.release.opened, true);
    assert.equal(final.release.compartmentId, 'BOX-A');
    assert.equal(controller.getSheet('P-001').status, SheetStatus.DELIVERED);
    assert.equal(gate.openCount, 1);
    assert.deepEqual(gate.calls[0].context, { paperId: 'P-001', ownerId: ALICE, jobId: 'J-1' });
  });

  test('A2: 单面任务显式声明空白背面，无需背面实印即可开格', () => {
    regSimplex(controller, 'P-010', ALICE, 'J-10', 'BOX-B');
    controller.handle(sideImaged({ paperId: 'P-010', ownerId: ALICE, jobId: 'J-10', side: Side.FRONT }, 'img-f10'));
    controller.handle(paperOutput({ paperId: 'P-010', sensorId: 'SENSOR-1' }, 'out-10'));
    const final = controller.handle(pickupAuthorized({ paperId: 'P-010', ownerId: ALICE }, 'auth-10'));

    assert.equal(final.release.opened, true);
    assert.equal(controller.getSheet('P-010').backPolicy, BackPolicy.BLANK);
    assert.equal(gate.openCount, 1);
  });

  test('A3: 事件乱序到达（授权/出纸先于成像），齐备前不开格，齐备后开格一次', () => {
    regDuplex(controller, 'P-020');
    controller.handle(pickupAuthorized({ paperId: 'P-020', ownerId: ALICE }, 'auth-20'));
    controller.handle(paperOutput({ paperId: 'P-020', sensorId: 'SENSOR-1' }, 'out-20'));
    const back = controller.handle(sideImaged({ paperId: 'P-020', ownerId: ALICE, jobId: 'J-1', side: Side.BACK }, 'img-b20'));
    assert.equal(back.release.opened, false); // 还缺正面
    const front = controller.handle(sideImaged({ paperId: 'P-020', ownerId: ALICE, jobId: 'J-1', side: Side.FRONT }, 'img-f20'));
    assert.equal(front.release.opened, true);
    assert.equal(gate.openCount, 1);
  });

  test('A4: 卡纸后新印使用新纸号正常交付；旧纸隔离直至回收确认，全程不开格', () => {
    // 旧纸 P-OLD：只完成正面即卡纸
    regDuplex(controller, 'P-OLD', ALICE, 'J-1', 'BOX-A');
    controller.handle(sideImaged({ paperId: 'P-OLD', ownerId: ALICE, jobId: 'J-1', side: Side.FRONT }, 'old-f'));
    const jam = controller.handle(paperJammed({ paperId: 'P-OLD' }, 'jam-old'));
    assert.equal(jam.status, SheetStatus.QUARANTINED);
    assert.equal(gate.openCount, 0);

    // 重印必须使用新纸号
    regDuplex(controller, 'P-NEW', ALICE, 'J-1', 'BOX-A');
    controller.handle(sideImaged({ paperId: 'P-NEW', ownerId: ALICE, jobId: 'J-1', side: Side.FRONT }, 'new-f'));
    controller.handle(sideImaged({ paperId: 'P-NEW', ownerId: ALICE, jobId: 'J-1', side: Side.BACK }, 'new-b'));
    controller.handle(paperOutput({ paperId: 'P-NEW', sensorId: 'SENSOR-1' }, 'new-out'));
    const delivered = controller.handle(pickupAuthorized({ paperId: 'P-NEW', ownerId: ALICE }, 'new-auth'));
    assert.equal(delivered.release.opened, true);
    assert.equal(gate.openCount, 1);

    // 旧纸未经回收确认仍隔离
    assert.equal(controller.getSheet('P-OLD').status, SheetStatus.QUARANTINED);
    const recycled = controller.handle(paperRecycled({ paperId: 'P-OLD' }, 'rec-old'));
    assert.equal(recycled.status, SheetStatus.RECYCLED);
    assert.equal(gate.openCount, 1); // 回收不开格
  });

  // ----------------------------------------------------- B. 应被阻止

  test('B1: 缺少背面/出纸/授权任一证据都不得开格', () => {
    regDuplex(controller, 'P-100');
    controller.handle(sideImaged({ paperId: 'P-100', ownerId: ALICE, jobId: 'J-1', side: Side.FRONT }, 'f100'));
    controller.handle(sideImaged({ paperId: 'P-100', ownerId: ALICE, jobId: 'J-1', side: Side.BACK }, 'b100'));
    // 缺出纸与授权
    controller.handle(pickupAuthorized({ paperId: 'P-100', ownerId: ALICE }, 'a100'));
    assert.equal(controller.getSheet('P-100').status, SheetStatus.REGISTERED);
    assert.equal(gate.openCount, 0);

    // 补出纸后才开格
    const r = controller.handle(paperOutput({ paperId: 'P-100', sensorId: 'SENSOR-1' }, 'o100'));
    assert.equal(r.release.opened, true);
    assert.equal(gate.openCount, 1);
  });

  test('B2: 他人取件授权被拒绝，绝不打开归属人的格子', () => {
    regDuplex(controller, 'P-101', ALICE, 'J-1', 'BOX-A');
    assert.throws(
      () => controller.handle(pickupAuthorized({ paperId: 'P-101', ownerId: BOB }, 'bob-auth')),
      (err) => err instanceof PaperControllerError && err.code === ErrorCode.WRONG_OWNER,
    );
    assert.equal(controller.getSheet('P-101').authorization, null);
    assert.equal(gate.openCount, 0);
  });

  test('B3: 旧纸迟到的半面回执不能补齐另一张纸（paperId 不匹配 / 归属不符均拒绝）', () => {
    regDuplex(controller, 'P-OLD', ALICE, 'J-1', 'BOX-A');
    controller.handle(sideImaged({ paperId: 'P-OLD', ownerId: ALICE, jobId: 'J-1', side: Side.FRONT }, 'old-f'));
    controller.handle(paperJammed({ paperId: 'P-OLD' }, 'jam'));
    regDuplex(controller, 'P-NEW', ALICE, 'J-1', 'BOX-A');

    // 1) 迟到回执仍写旧纸号：旧纸隔离，入口直接拒绝
    assert.throws(
      () => controller.handle(sideImaged({ paperId: 'P-OLD', ownerId: ALICE, jobId: 'J-1', side: Side.BACK }, 'late-back')),
      (err) => err.code === ErrorCode.PAPER_QUARANTINED,
    );

    // 2) 迟到回执被错误指向新纸号但归属/任务对不上：跨纸补齐被拒
    assert.throws(
      () => controller.handle(sideImaged({ paperId: 'P-NEW', ownerId: BOB, jobId: 'J-9', side: Side.BACK }, 'evil-back')),
      (err) => err.code === ErrorCode.WRONG_PAPER_OWNER,
    );

    // 3) 指向根本不存在的纸号：拒绝
    assert.throws(
      () => controller.handle(sideImaged({ paperId: 'P-GHOST', ownerId: ALICE, jobId: 'J-1', side: Side.BACK }, 'ghost')),
      (err) => err.code === ErrorCode.UNKNOWN_PAPER,
    );

    // 新纸证据未齐，保持未交付
    assert.equal(controller.getSheet('P-NEW').status, SheetStatus.REGISTERED);
    assert.deepEqual(controller.getSheet('P-NEW').imagedSides, []);
    assert.equal(gate.openCount, 0);
  });

  test('B4: 隔离旧纸的出纸/授权事件一律拒绝，补齐也不能开格', () => {
    regDuplex(controller, 'P-OLD', ALICE, 'J-1', 'BOX-A');
    controller.handle(paperJammed({ paperId: 'P-OLD' }, 'jam'));
    assert.throws(
      () => controller.handle(paperOutput({ paperId: 'P-OLD', sensorId: 'SENSOR-1' }, 'out-old')),
      (err) => err.code === ErrorCode.PAPER_QUARANTINED,
    );
    assert.throws(
      () => controller.handle(pickupAuthorized({ paperId: 'P-OLD', ownerId: ALICE }, 'auth-old')),
      (err) => err.code === ErrorCode.PAPER_QUARANTINED,
    );
    assert.equal(gate.openCount, 0);
  });

  test('B5: 未在册 / 已移除传感器的出纸上报被拒绝，替换后新上报才有效', () => {
    regDuplex(controller, 'P-200');
    assert.throws(
      () => controller.handle(paperOutput({ paperId: 'P-200', sensorId: 'ROGUE-SENSOR' }, 'rogue')),
      (err) => err.code === ErrorCode.UNKNOWN_SENSOR,
    );

    controller.removeSensor('SENSOR-1');
    assert.throws(
      () => controller.handle(paperOutput({ paperId: 'P-200', sensorId: 'SENSOR-1' }, 'out-removed')),
      (err) => err.code === ErrorCode.UNKNOWN_SENSOR,
    );
    assert.equal(controller.getSheet('P-200').output, null);

    // 替换新传感器硬件后恢复
    const sensor2 = new OutputSensor('SENSOR-2');
    controller.registerSensor(sensor2);
    const r = controller.handle(paperOutput({ paperId: 'P-200', sensorId: 'SENSOR-2' }, 'out-200'));
    assert.equal(r.accepted, true);
    assert.equal(controller.getSheet('P-200').output.sensorId, 'SENSOR-2');
  });

  test('B6: 单面纸不得登记背面实印；登记时未显式声明背面策略也被拒绝', () => {
    regSimplex(controller, 'P-300', ALICE, 'J-30', 'BOX-C');
    controller.handle(sideImaged({ paperId: 'P-300', ownerId: ALICE, jobId: 'J-30', side: Side.FRONT }, 'f300'));
    assert.throws(
      () => controller.handle(sideImaged({ paperId: 'P-300', ownerId: ALICE, jobId: 'J-30', side: Side.BACK }, 'b300')),
      (err) => err.code === ErrorCode.SIDE_MISMATCH,
    );

    assert.throws(
      () => controller.handle({
        eventId: 'bad-reg',
        type: 'PAPER_REGISTERED',
        paperId: 'P-301', ownerId: ALICE, jobId: 'J-31', compartmentId: 'BOX-C',
      }),
      (err) => err.code === ErrorCode.INVALID_SIDE_POLICY,
    );
  });

  test('B7: 实体纸号不可复用，重复登记被拒绝', () => {
    regDuplex(controller, 'P-400');
    assert.throws(
      () => controller.handle(
        paperRegistered({ paperId: 'P-400', ownerId: ALICE, jobId: 'J-1', compartmentId: 'BOX-A', backPolicy: BackPolicy.PRINTED },
          'reg-P-400-dup'),
      ),
      (err) => err.code === ErrorCode.PAPER_ALREADY_REGISTERED,
    );
  });

  // ----------------------------------------------------- C. 边界 / 交错

  test('C1: 重复 eventId 重放返回首次结果且无副作用，闸门始终只开一次', () => {
    regDuplex(controller, 'P-500');
    controller.handle(sideImaged({ paperId: 'P-500', ownerId: ALICE, jobId: 'J-1', side: Side.FRONT }, 'f500'));
    controller.handle(sideImaged({ paperId: 'P-500', ownerId: ALICE, jobId: 'J-1', side: Side.BACK }, 'b500'));
    // 交付前先上报一次出纸，再用不同 eventId 重复上报：accepted=false 且只留一条事实
    controller.handle(paperOutput({ paperId: 'P-500', sensorId: 'SENSOR-1' }, 'o500'));
    const dupBeforeDelivery = controller.handle(paperOutput({ paperId: 'P-500', sensorId: 'SENSOR-1' }, 'o500-dup'));
    assert.equal(dupBeforeDelivery.accepted, false);
    assert.equal(controller.getSheet('P-500').audit.filter((a) => a.type === 'PAPER_OUTPUT').length, 1);

    const first = controller.handle(pickupAuthorized({ paperId: 'P-500', ownerId: ALICE }, 'a500'));
    assert.equal(first.release.opened, true);

    // 同一 eventId 重复上报（模拟网络重投 / 传感器抖动）：回放首次结果，无副作用
    const replay1 = controller.handle(pickupAuthorized({ paperId: 'P-500', ownerId: ALICE }, 'a500'));
    const replay2 = controller.handle(paperOutput({ paperId: 'P-500', sensorId: 'SENSOR-1' }, 'o500'));
    assert.deepEqual(replay1, first);
    assert.equal(replay2.eventId, 'o500');
    assert.equal(controller.getSheet('P-500').audit.filter((a) => a.type === 'PAPER_OUTPUT').length, 1);
    assert.equal(gate.openCount, 1);
    assert.equal(controller.getSheet('P-500').audit.filter((a) => a.type === 'COMPARTMENT_OPENED').length, 1);

    // 交付是硬终态：任何新 eventId 的证据事件一律拒绝，而非二次开格
    assert.throws(
      () => controller.handle(paperOutput({ paperId: 'P-500', sensorId: 'SENSOR-1' }, 'o500-after')),
      (err) => err.code === ErrorCode.PAPER_DELIVERED,
    );
    assert.equal(gate.openCount, 1);
  });

  test('C2: 完整事件日志在全新控制器上二次重放，终态一致且每张纸只开一格', () => {
    const log = [
      paperRegistered({ paperId: 'P-600', ownerId: ALICE, jobId: 'J-1', compartmentId: 'BOX-A', backPolicy: BackPolicy.PRINTED }, 'e1'),
      sideImaged({ paperId: 'P-600', ownerId: ALICE, jobId: 'J-1', side: Side.FRONT }, 'e2'),
      paperJammed({ paperId: 'P-600' }, 'e3'),
      paperRecycled({ paperId: 'P-600' }, 'e4'),
      paperRegistered({ paperId: 'P-601', ownerId: ALICE, jobId: 'J-1', compartmentId: 'BOX-A', backPolicy: BackPolicy.PRINTED }, 'e5'),
      sideImaged({ paperId: 'P-601', ownerId: ALICE, jobId: 'J-1', side: Side.FRONT }, 'e6'),
      sideImaged({ paperId: 'P-601', ownerId: ALICE, jobId: 'J-1', side: Side.BACK }, 'e7'),
      paperOutput({ paperId: 'P-601', sensorId: 'SENSOR-1' }, 'e8'),
      pickupAuthorized({ paperId: 'P-601', ownerId: ALICE }, 'e9'),
    ];
    // 交错加入重复 eventId（回放，无副作用）与同内容新 eventId 的重复登记（应被拒绝）
    const shuffled = [log[4], log[0], log[1], log[8], log[2], log[5], log[3], log[6], log[8], log[7], log[4],
      paperRegistered({ paperId: 'P-601', ownerId: ALICE, jobId: 'J-1', compartmentId: 'BOX-A', backPolicy: BackPolicy.PRINTED }, 'e5-dup')];

    const gate2 = new RecordingGate();
    const c2 = makeController({ gate: gate2, sensor: new OutputSensor('SENSOR-1') });
    const { errors } = replay(c2, shuffled);
    // 同内容、新 eventId 的重复登记应被拒绝并记录；重复 eventId（e5 首次出现的回放）不报错
    assert.ok(errors.some((e) => e.event.eventId === 'e5-dup' && e.code === ErrorCode.PAPER_ALREADY_REGISTERED));
    assert.ok(!errors.some((e) => e.event.eventId === 'e5'));
    assert.equal(c2.getSheet('P-600').status, SheetStatus.RECYCLED);
    assert.equal(c2.getSheet('P-601').status, SheetStatus.DELIVERED);
    assert.equal(gate2.openCount, 1);

    // 同一日志在第三个控制器上再重放一次：终态必须完全一致
    const gate3 = new RecordingGate();
    const c3 = makeController({ gate: gate3, sensor: new OutputSensor('SENSOR-1') });
    replay(c3, shuffled);
    assert.deepEqual(
      c3.listSheets().map((s) => ({ paperId: s.paperId, status: s.status })),
      c2.listSheets().map((s) => ({ paperId: s.paperId, status: s.status })),
    );
    assert.equal(gate3.openCount, gate2.openCount);
  });

  test('C3: 交错场景——旧纸半面回执与新纸事件交替到达，旧纸永远无法凑齐证据', () => {
    regDuplex(controller, 'P-OLD', ALICE, 'J-1', 'BOX-A');
    controller.handle(sideImaged({ paperId: 'P-OLD', ownerId: ALICE, jobId: 'J-1', side: Side.FRONT }, 'of'));
    controller.handle(paperJammed({ paperId: 'P-OLD' }, 'jam'));
    regDuplex(controller, 'P-NEW', ALICE, 'J-1', 'BOX-A');

    // 交替：新纸正面 → 旧纸迟到背面（拒）→ 新纸背面 → 旧纸迟到出纸（拒）→ …
    controller.handle(sideImaged({ paperId: 'P-NEW', ownerId: ALICE, jobId: 'J-1', side: Side.FRONT }, 'nf'));
    assert.throws(() => controller.handle(
      sideImaged({ paperId: 'P-OLD', ownerId: ALICE, jobId: 'J-1', side: Side.BACK }, 'ob'),
    ), (e) => e.code === ErrorCode.PAPER_QUARANTINED);
    controller.handle(sideImaged({ paperId: 'P-NEW', ownerId: ALICE, jobId: 'J-1', side: Side.BACK }, 'nb'));
    assert.throws(() => controller.handle(
      paperOutput({ paperId: 'P-OLD', sensorId: 'SENSOR-1' }, 'oo'),
    ), (e) => e.code === ErrorCode.PAPER_QUARANTINED);
    controller.handle(paperOutput({ paperId: 'P-NEW', sensorId: 'SENSOR-1' }, 'no'));
    const done = controller.handle(pickupAuthorized({ paperId: 'P-NEW', ownerId: ALICE }, 'na'));

    assert.equal(done.release.opened, true);
    assert.equal(controller.getSheet('P-OLD').status, SheetStatus.QUARANTINED);
    assert.deepEqual(controller.getSheet('P-OLD').imagedSides, [Side.FRONT]);
    assert.equal(gate.openCount, 1); // 只有新纸开过格
  });

  test('C4: 闸门故障时事件不入账，修复后重投同一事件成功且只开一次', () => {
    const flakyGate = new RecordingGate({ failTimes: 1 });
    const c = makeController({ gate: flakyGate, sensor });
    regDuplex(c, 'P-700');
    c.handle(sideImaged({ paperId: 'P-700', ownerId: ALICE, jobId: 'J-1', side: Side.FRONT }, 'f700'));
    c.handle(sideImaged({ paperId: 'P-700', ownerId: ALICE, jobId: 'J-1', side: Side.BACK }, 'b700'));
    c.handle(paperOutput({ paperId: 'P-700', sensorId: 'SENSOR-1' }, 'o700'));

    assert.throws(
      () => c.handle(pickupAuthorized({ paperId: 'P-700', ownerId: ALICE }, 'a700')),
      (err) => err.code === ErrorCode.GATE_FAILED,
    );
    // 失败后状态不变、授权未入账（事件未进重放表）、无成功开格
    assert.equal(c.getSheet('P-700').status, SheetStatus.REGISTERED);
    assert.equal(c.getSheet('P-700').authorization, null);
    assert.equal(flakyGate.openCount, 0);
    assert.ok(!c.getProcessedEventIds().includes('a700'));

    // 运行中替换为正常闸门，重投同一事件
    c.setGate(gate);
    const retried = c.handle(pickupAuthorized({ paperId: 'P-700', ownerId: ALICE }, 'a700'));
    assert.equal(retried.release.opened, true);
    assert.equal(gate.openCount, 1);
    assert.equal(flakyGate.openCount, 0);
  });

  test('C5: 运行中替换开格回调，后续开格走新回调；已交付纸不受影响', () => {
    regDuplex(controller, 'P-800');
    controller.handle(sideImaged({ paperId: 'P-800', ownerId: ALICE, jobId: 'J-1', side: Side.FRONT }, 'f800'));
    controller.handle(sideImaged({ paperId: 'P-800', ownerId: ALICE, jobId: 'J-1', side: Side.BACK }, 'b800'));
    controller.handle(paperOutput({ paperId: 'P-800', sensorId: 'SENSOR-1' }, 'o800'));

    const newGate = new RecordingGate();
    controller.setGate(newGate);
    const r = controller.handle(pickupAuthorized({ paperId: 'P-800', ownerId: ALICE }, 'a800'));
    assert.equal(r.release.opened, true);
    assert.equal(newGate.openCount, 1);
    assert.equal(gate.openCount, 0);

    // 已交付纸再收到任何证据事件都被拒绝
    assert.throws(
      () => controller.handle(paperOutput({ paperId: 'P-800', sensorId: 'SENSOR-1' }, 'o800-again')),
      (err) => err.code === ErrorCode.PAPER_DELIVERED,
    );
  });

  test('C6: 两人各自的纸独立核验，一人开格不影响另一人的隔离与授权判断', () => {
    // Alice 的纸卡纸隔离
    regDuplex(controller, 'P-A', ALICE, 'J-A', 'BOX-A');
    controller.handle(paperJammed({ paperId: 'P-A' }, 'jam-a'));

    // Bob 的单面纸正常交付到 BOX-B
    regSimplex(controller, 'P-B', BOB, 'J-B', 'BOX-B');
    controller.handle(sideImaged({ paperId: 'P-B', ownerId: BOB, jobId: 'J-B', side: Side.FRONT }, 'bf'));
    controller.handle(paperOutput({ paperId: 'P-B', sensorId: 'SENSOR-1' }, 'bo'));
    const r = controller.handle(pickupAuthorized({ paperId: 'P-B', ownerId: BOB }, 'ba'));

    assert.equal(r.release.opened, true);
    assert.equal(r.release.compartmentId, 'BOX-B');
    assert.equal(controller.getSheet('P-A').status, SheetStatus.QUARANTINED);
    assert.equal(controller.getSheet('P-B').status, SheetStatus.DELIVERED);
    assert.equal(gate.openCount, 1);
    assert.equal(gate.calls[0].compartmentId, 'BOX-B');

    // Alice 不能用自己授权开 Bob 的纸（已交付也直接拒绝）
    assert.throws(
      () => controller.handle(pickupAuthorized({ paperId: 'P-B', ownerId: ALICE }, 'cross-auth')),
      (err) => err.code === ErrorCode.PAPER_DELIVERED,
    );
  });
});
