import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import {
  FirstReportBoard,
  PHASE,
  ADJUDICATION_STATUS,
  computeCommitment,
  toHex,
  DepositWindowClosedError,
  RevealNotOpenError,
  RevealWindowClosedError,
  CommitmentMismatchError,
  DepositAlreadyRevealedError,
} from '../src/index.mjs';

// 时间轴（注入时钟，确定性测试）：
// t=10 存证期；t=100 揭示窗口开启（存证截止）；
// t=150 窗口内；t=200 截止时刻（仍可揭示）；t=201 已过期。
const OPEN = 100;
const DEADLINE = 200;

function makeBoard() {
  let current = 10;
  const clock = () => current;
  const board = new FirstReportBoard({
    revealOpensAt: OPEN,
    revealDeadline: DEADLINE,
    clock,
  });
  return {
    board,
    setAt: (t) => {
      current = t;
    },
  };
}

function preimage(overrides = {}) {
  return {
    project: 'phoenix',
    reporter: 'alice',
    finding: 'F-001',
    salt: 'salt-a',
    ...overrides,
  };
}

describe('首报裁决：存证/揭示生命周期与优先权', () => {
  let ctx;
  beforeEach(() => {
    ctx = makeBoard();
  });

  test('可接受结果：较早轮次按期匹配揭示者获得首报资格', () => {
    const { board, setAt } = ctx;
    const c1 = computeCommitment(preimage({ reporter: 'alice', salt: 'sa' }));
    const c2 = computeCommitment(preimage({ reporter: 'bob', salt: 'sb' }));
    setAt(10);
    const d1 = board.deposit({ round: 1, digest: c1, depositId: 'd1' });
    const d2 = board.deposit({ round: 2, digest: c2, depositId: 'd2' });
    // 存证期不保存任何明文身份
    assert.equal(d1.reporter, undefined);
    assert.equal(d2.reporter, undefined);
    assert.ok(d1.seq < d2.seq);

    setAt(150);
    board.reveal('d1', preimage({ reporter: 'alice', salt: 'sa' }));
    board.reveal('d2', preimage({ reporter: 'bob', salt: 'sb' }));

    const result = board.adjudicate();
    assert.equal(result.status, ADJUDICATION_STATUS.PROVISIONAL);
    assert.equal(result.findings.length, 1);
    const group = result.findings[0];
    assert.equal(group.winnerRound, 1);
    assert.deepEqual(group.winners.map((w) => w.depositId), ['d1']);
    assert.equal(group.tied, false);
  });

  test('可接受结果：同一轮同指纹的有效存证并列，接收序号不决定优先权', () => {
    const { board, setAt } = ctx;
    const ca = computeCommitment(preimage({ reporter: 'alice', salt: 'sa' }));
    const cb = computeCommitment(preimage({ reporter: 'bob', salt: 'sb' }));
    setAt(20);
    const a = board.deposit({ round: 3, digest: ca, depositId: 'a' });
    const b = board.deposit({ round: 3, digest: cb, depositId: 'b' });
    assert.notEqual(a.seq, b.seq);

    setAt(150);
    board.reveal('a', preimage({ reporter: 'alice', salt: 'sa' }));
    board.reveal('b', preimage({ reporter: 'bob', salt: 'sb' }));

    const result = board.adjudicate();
    const group = result.findings[0];
    assert.equal(group.tied, true);
    assert.equal(group.winnerRound, 3);
    assert.deepEqual(
      group.winners.map((w) => w.reporter).sort(),
      ['alice', 'bob'],
    );
  });

  test('应被阻止：揭示内容与存证摘要不匹配时拒绝，且不写入明文', () => {
    const { board, setAt } = ctx;
    const c1 = computeCommitment(preimage());
    setAt(10);
    board.deposit({ round: 1, digest: c1, depositId: 'd1' });

    setAt(150);
    assert.throws(
      () => board.reveal('d1', preimage({ salt: 'wrong-salt' })),
      CommitmentMismatchError,
    );
    const record = board.getInternalRecord('d1');
    assert.equal(record.revealed, false);
    assert.equal(record.eligible, false);
    assert.equal(record.salt, null); // 错误盐不得落库
    const audit = board.getAuditLog();
    assert.ok(audit.some((e) => e.type === 'commitment-mismatch'));
    // 审计事件不含明文
    assert.equal(JSON.stringify(audit).includes('wrong-salt'), false);
  });

  test('应被阻止：晚存证者先揭示不能抢先——窗口外揭示被拒，过期后剔除', () => {
    const { board, setAt } = ctx;
    const lateCommitment = computeCommitment(preimage({ reporter: 'mallory', salt: 'sm' }));
    setAt(10);
    board.deposit({ round: 1, digest: lateCommitment, depositId: 'late' });

    // 期限已过才揭示
    setAt(DEADLINE + 1);
    assert.throws(
      () => board.reveal('late', preimage({ reporter: 'mallory', salt: 'sm' })),
      RevealWindowClosedError,
    );

    const result = board.adjudicate();
    assert.equal(result.status, ADJUDICATION_STATUS.FINAL);
    assert.equal(result.findings.length, 0); // 未揭示存证被剔除，无人获胜
    assert.equal(result.counts.purgedUnrevealed, 1);
    assert.equal(result.counts.unrevealed, 0);
  });

  test('应被阻止：存证窗口关闭后新存证直接拒绝', () => {
    const { board, setAt } = ctx;
    setAt(OPEN); // 已进入揭示窗口
    assert.throws(
      () => board.deposit({ round: 1, digest: Buffer.alloc(32) }),
      DepositWindowClosedError,
    );
    setAt(DEADLINE + 1);
    assert.throws(
      () => board.deposit({ round: 1, digest: Buffer.alloc(32) }),
      DepositWindowClosedError,
    );
  });

  test('应被阻止：揭示窗口未开启时揭示被拒绝', () => {
    const { board, setAt } = ctx;
    setAt(10);
    board.deposit({ round: 1, digest: computeCommitment(preimage()), depositId: 'd1' });
    assert.throws(() => board.reveal('d1', preimage()), RevealNotOpenError);
  });

  test('边界：截止时刻 t=deadline 的揭示仍有效，t=deadline+1 被拒且定案', () => {
    const { board, setAt } = ctx;
    const c1 = computeCommitment(preimage());
    const c2 = computeCommitment(preimage({ reporter: 'carol', salt: 'sc' }));
    setAt(OPEN - 1);
    board.deposit({ round: 1, digest: c1, depositId: 'd1' });
    board.deposit({ round: 1, digest: c2, depositId: 'd2' });

    setAt(DEADLINE);
    assert.equal(board.phase(), PHASE.REVEAL);
    const view = board.reveal('d1', preimage());
    assert.equal(view.revealed, true);
    // 截止时刻窗口尚未“过去”，结果仍为暂定；d2 未揭示阻断定案
    const atDeadline = board.adjudicate();
    assert.equal(atDeadline.status, ADJUDICATION_STATUS.PROVISIONAL);
    assert.equal(atDeadline.findings[0].winnerRound, 1);
    assert.equal(atDeadline.findings[0].blockedByEarlierDeposit, true);

    // 期限一过：对仍未揭示的 d2 晚揭示被拒，d2 被剔除后定案
    setAt(DEADLINE + 1);
    assert.equal(board.phase(), PHASE.CLOSED);
    assert.throws(
      () => board.reveal('d2', preimage({ reporter: 'carol', salt: 'sc' })),
      RevealWindowClosedError,
    );
    const result = board.adjudicate();
    assert.equal(result.status, ADJUDICATION_STATUS.FINAL);
    assert.equal(result.findings[0].winnerRound, 1);
    assert.equal(result.findings[0].blockedByEarlierDeposit, false);
    assert.equal(result.counts.purgedUnrevealed, 1);
  });

  test('边界：revealOpensAt 时刻存证关闭、揭示开启', () => {
    const { board, setAt } = ctx;
    setAt(OPEN - 1);
    assert.equal(board.phase(), PHASE.DEPOSIT);
    setAt(OPEN);
    assert.equal(board.phase(), PHASE.REVEAL);
    assert.throws(
      () => board.deposit({ round: 1, digest: Buffer.alloc(32) }),
      DepositWindowClosedError,
    );
  });

  test('交错场景：早存证未揭示时只能发布暂定结果，且阻断同轮/更早轮次的定案', () => {
    const { board, setAt } = ctx;
    const cEarly = computeCommitment(preimage({ reporter: 'alice', salt: 'sa' }));
    const cLater = computeCommitment(preimage({ reporter: 'bob', salt: 'sb' }));
    setAt(10);
    board.deposit({ round: 1, digest: cEarly, depositId: 'early' });
    board.deposit({ round: 2, digest: cLater, depositId: 'later' });

    setAt(150);
    board.reveal('later', preimage({ reporter: 'bob', salt: 'sb' }));

    const provisional = board.adjudicate();
    assert.equal(provisional.status, ADJUDICATION_STATUS.PROVISIONAL);
    const group = provisional.findings[0];
    assert.equal(group.status, ADJUDICATION_STATUS.PROVISIONAL);
    assert.equal(group.blockedByEarlierDeposit, true);
    // 揭示再晚也不改变优先权：当前获胜者仍是已揭示的唯一有效存证
    assert.deepEqual(group.winners.map((w) => w.depositId), ['later']);

    // 期限过后早存证被剔除，结果定案
    setAt(DEADLINE + 1);
    const finalResult = board.adjudicate();
    assert.equal(finalResult.status, ADJUDICATION_STATUS.FINAL);
    assert.equal(finalResult.findings[0].blockedByEarlierDeposit, false);
    assert.equal(finalResult.findings[0].winners[0].depositId, 'later');
    assert.equal(finalResult.counts.purgedUnrevealed, 1);
  });

  test('交错场景：晚轮次先揭示不能超越尚未揭示的早轮次承诺的暂定地位', () => {
    const { board, setAt } = ctx;
    // round 1 的承诺一直不揭示；round 5 先揭示
    const c1 = computeCommitment(preimage({ reporter: 'alice', salt: 'sa' }));
    const c5 = computeCommitment(preimage({ reporter: 'bob', salt: 'sb' }));
    setAt(10);
    board.deposit({ round: 1, digest: c1, depositId: 'r1' });
    board.deposit({ round: 5, digest: c5, depositId: 'r5' });
    setAt(150);
    board.reveal('r5', preimage({ reporter: 'bob', salt: 'sb' }));

    const provisional = board.adjudicate();
    assert.equal(provisional.findings[0].winnerRound, 5);
    assert.equal(provisional.findings[0].blockedByEarlierDeposit, true);

    // 窗口结束前 r1 补揭示（按期）：round 1 立刻成为获胜者
    setAt(DEADLINE);
    board.reveal('r1', preimage({ reporter: 'alice', salt: 'sa' }));
    const after = board.adjudicate();
    assert.equal(after.findings[0].winnerRound, 1);
    assert.deepEqual(after.findings[0].winners.map((w) => w.depositId), ['r1']);

    setAt(DEADLINE + 1);
    const finalResult = board.adjudicate();
    assert.equal(finalResult.status, ADJUDICATION_STATUS.FINAL);
    assert.equal(finalResult.findings[0].winnerRound, 1);
  });

  test('交错场景：不同发现指纹独立裁决，互不影响', () => {
    const { board, setAt } = ctx;
    const cX = computeCommitment(preimage({ finding: 'X', reporter: 'alice', salt: 'sa' }));
    const cY = computeCommitment(preimage({ finding: 'Y', reporter: 'bob', salt: 'sb' }));
    setAt(10);
    board.deposit({ round: 2, digest: cX, depositId: 'x' });
    board.deposit({ round: 1, digest: cY, depositId: 'y' });
    setAt(150);
    board.reveal('x', preimage({ finding: 'X', reporter: 'alice', salt: 'sa' }));
    board.reveal('y', preimage({ finding: 'Y', reporter: 'bob', salt: 'sb' }));

    const result = board.adjudicate();
    assert.equal(result.findings.length, 2);
    const byFinding = Object.fromEntries(result.findings.map((f) => [f.finding, f]));
    assert.equal(byFinding.X.winnerRound, 2);
    assert.equal(byFinding.Y.winnerRound, 1);
    assert.deepEqual(byFinding.X.winners.map((w) => w.reporter), ['alice']);
    assert.deepEqual(byFinding.Y.winners.map((w) => w.reporter), ['bob']);
  });

  test('隐私交叉约束：外部回执只含已揭示身份，未揭示记录与盐一律不暴露', () => {
    const { board, setAt } = ctx;
    const c1 = computeCommitment(preimage({ reporter: 'alice', salt: 'sa' }));
    const c2 = computeCommitment(preimage({ reporter: 'bob', salt: 'sb' }));
    setAt(10);
    board.deposit({ round: 1, digest: c1, depositId: 'd1' });
    board.deposit({ round: 1, digest: c2, depositId: 'd2' });
    setAt(150);
    board.reveal('d1', preimage({ reporter: 'alice', salt: 'sa' }));

    const receipt = board.getReceipt();
    const serialized = JSON.stringify(receipt);
    // 盐在任何情况下都不得出现在外部回执
    assert.equal(serialized.includes('sa'), false);
    assert.equal(serialized.includes('sb'), false);
    const d1View = receipt.deposits.find((d) => d.depositId === 'd1');
    const d2View = receipt.deposits.find((d) => d.depositId === 'd2');
    assert.equal(d1View.revealed, true);
    assert.equal(d1View.reporter, 'alice'); // 已揭示：身份可见
    assert.equal(d1View.salt, undefined);
    assert.equal(d2View.revealed, false);
    assert.equal(d2View.reporter, undefined); // 未揭示：身份缺席
    assert.equal(d2View.project, undefined);
    assert.equal(d2View.finding, undefined);
    assert.equal(d2View.digest, toHex(c2)); // 仅保留公开摘要元数据
    assert.equal(receipt.counts.revealed, 1);
    assert.equal(receipt.counts.unrevealed, 1);

    // 过期定案后的回执仍不泄露被剔除存证的身份
    setAt(DEADLINE + 1);
    const finalReceipt = board.getReceipt();
    const purged = finalReceipt.deposits.find((d) => d.depositId === 'd2');
    assert.equal(purged.revealed, false);
    assert.equal(purged.reporter, undefined);
    assert.equal(JSON.stringify(finalReceipt).includes('sb'), false);
  });

  test('重复揭示被拒绝且不覆盖首次揭示数据', () => {
    const { board, setAt } = ctx;
    const c1 = computeCommitment(preimage());
    setAt(10);
    board.deposit({ round: 1, digest: c1, depositId: 'd1' });
    setAt(150);
    board.reveal('d1', preimage());
    assert.throws(() => board.reveal('d1', preimage()), DepositAlreadyRevealedError);
    assert.equal(board.getInternalRecord('d1').reporter, 'alice');
  });
});
