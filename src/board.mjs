// 首报裁决板：
// 1. 存证阶段只接收保密摘要（承诺），不保存任何明文内容；
// 2. 揭示窗口内提交明文，重算摘要匹配后方为有效存证；
// 3. 优先权按存证轮次排序，同轮同指纹并列；接收序号仅用于标识；
// 4. 窗口内只能发布暂定结果；期限一过剔除未揭示存证后定案；
// 5. 外部回执只暴露已揭示存证的身份，盐永不出现在任何外部视图中。
import { computeCommitment, safeEqualDigest, toHex } from './digest.mjs';
import {
  DepositWindowClosedError,
  RevealNotOpenError,
  RevealWindowClosedError,
  CommitmentMismatchError,
  DepositAlreadyRevealedError,
  UnknownDepositError,
} from './errors.mjs';

export const PHASE = Object.freeze({
  DEPOSIT: 'deposit',
  REVEAL: 'reveal',
  CLOSED: 'closed',
});

export const ADJUDICATION_STATUS = Object.freeze({
  PROVISIONAL: 'provisional',
  FINAL: 'final',
});

function assertPositiveInteger(value, label) {
  if (!Number.isInteger(value) || value <= 0) {
    throw new TypeError(`${label} 必须是正整数`);
  }
}

function normalizeDigest(digest) {
  if (Buffer.isBuffer(digest)) {
    if (digest.length !== 32) {
      throw new TypeError('存证摘要必须是 32 字节 Buffer 或 64 位十六进制字符串');
    }
    return Buffer.from(digest);
  }
  if (typeof digest === 'string' && /^[0-9a-fA-F]{64}$/.test(digest)) {
    return Buffer.from(digest, 'hex');
  }
  throw new TypeError('存证摘要必须是 32 字节 Buffer 或 64 位十六进制字符串');
}

/**
 * 仅含可公开字段的存证视图。未揭示存证不含任何身份/内容字段，
 * 已揭示存证也不含盐（salt 仅留在内部记录中）。
 */
function toPublicView(record) {
  const base = {
    depositId: record.id,
    seq: record.seq,
    round: record.round,
    digest: toHex(record.digest),
    depositedAt: record.depositedAt,
    revealed: record.revealed,
  };
  if (record.revealed) {
    return {
      ...base,
      revealedAt: record.revealedAt,
      project: record.project,
      reporter: record.reporter,
      finding: record.finding,
    };
  }
  return base;
}

export class FirstReportBoard {
  /**
   * @param {object} config
   * @param {number} config.revealOpensAt 揭示窗口开启时刻（同时是存证截止时刻）
   * @param {number} config.revealDeadline 揭示窗口截止时刻，该时刻仍可揭示
   * @param {() => number} [config.clock] 时间源，默认 Date.now；测试可注入单调时钟
   */
  constructor({ revealOpensAt, revealDeadline, clock } = {}) {
    assertPositiveInteger(revealOpensAt, 'revealOpensAt');
    assertPositiveInteger(revealDeadline, 'revealDeadline');
    if (revealDeadline <= revealOpensAt) {
      throw new RangeError('revealDeadline 必须晚于 revealOpensAt');
    }
    this.#revealOpensAt = revealOpensAt;
    this.#revealDeadline = revealDeadline;
    this.#clock = typeof clock === 'function' ? clock : () => Date.now();
  }

  #revealOpensAt;
  #revealDeadline;
  #clock;
  #records = new Map();
  #seqCounter = 0;
  #auditLog = [];

  get revealOpensAt() {
    return this.#revealOpensAt;
  }

  get revealDeadline() {
    return this.#revealDeadline;
  }

  now() {
    return this.#clock();
  }

  /** 当前阶段：deposit（存证期）/ reveal（揭示窗口）/ closed（已过期） */
  phase() {
    const now = this.now();
    if (now < this.#revealOpensAt) return PHASE.DEPOSIT;
    if (now <= this.#revealDeadline) return PHASE.REVEAL;
    return PHASE.CLOSED;
  }

  /**
   * 提交存证（仅保存摘要）。存证窗口在揭示窗口开启时关闭。
   * @param {{round: number, digest: Buffer|string, depositId?: string}} input
   * @returns {object} 公开存证视图
   */
  deposit({ round, digest, depositId } = {}) {
    if (this.phase() !== PHASE.DEPOSIT) {
      throw new DepositWindowClosedError();
    }
    assertPositiveInteger(round, 'round');
    const normalized = normalizeDigest(digest);
    const id = depositId ?? `dep-${this.#seqCounter + 1}`;
    if (this.#records.has(id)) {
      throw new RangeError(`存证标识 ${id} 已存在`);
    }
    const record = {
      id,
      seq: ++this.#seqCounter, // 接收序号：单调标识，不参与优先权排序
      round,
      digest: normalized,
      depositedAt: this.now(),
      revealed: false,
      revealedAt: null,
      eligible: false,
      project: null,
      reporter: null,
      finding: null,
      salt: null,
    };
    this.#records.set(id, record);
    return toPublicView(record);
  }

  /**
   * 便捷方法：本地由明文计算摘要后立即存证。
   * 板内仍只保存摘要，不保留任何明文。
   */
  depositPreimage({ round, project, reporter, finding, salt, depositId } = {}) {
    const commitment = computeCommitment({ project, reporter, finding, salt });
    const view = this.deposit({ round, digest: commitment, depositId });
    return { ...view, commitment };
  }

  /**
   * 在揭示窗口内揭示存证内容。晚揭示、不匹配、重复揭示均被拒绝并记入审计日志。
   * @param {string} depositId
   * @param {{project: string, reporter: string, finding: string, salt: string}} preimage
   * @returns {object} 已揭示存证的公开视图（不含盐）
   */
  reveal(depositId, preimage) {
    const record = this.#records.get(depositId);
    if (!record) {
      throw new UnknownDepositError();
    }
    if (record.revealed) {
      this.#audit('duplicate-reveal', depositId);
      throw new DepositAlreadyRevealedError();
    }
    const phase = this.phase();
    if (phase === PHASE.DEPOSIT) {
      throw new RevealNotOpenError();
    }
    if (phase === PHASE.CLOSED) {
      // 晚存证者先揭示不能抢先：窗口外揭示一律不受理，存证保持未揭示状态，
      // 期限过后将被剔除。
      this.#audit('late-reveal-rejected', depositId);
      throw new RevealWindowClosedError();
    }

    const candidate = computeCommitment(preimage);
    if (!safeEqualDigest(candidate, record.digest)) {
      // 不匹配：存证保持未揭示，不写入任何明文（含错误盐）。
      this.#audit('commitment-mismatch', depositId);
      throw new CommitmentMismatchError();
    }

    record.revealed = true;
    record.eligible = true;
    record.revealedAt = this.now();
    record.project = preimage.project;
    record.reporter = preimage.reporter;
    record.finding = preimage.finding;
    record.salt = preimage.salt; // 仅内部记录，外部视图与回执均不输出
    return toPublicView(record);
  }

  /**
   * 裁决：
   * - 存证期/揭示窗口内 → 暂定结果（provisional）；
   * - 过期后 → 先剔除未揭示存证，再按指纹分组定案（final）。
   * 优先权：有效存证按存证轮次升序，同轮同指纹并列；序号不影响优先权。
   */
  adjudicate() {
    const phase = this.phase();
    const now = this.now();
    const all = [...this.#records.values()];
    const eligible = all.filter((r) => r.eligible);
    const unrevealed = all.filter((r) => !r.revealed);

    const groups = new Map();
    for (const record of eligible) {
      if (!groups.has(record.finding)) groups.set(record.finding, []);
      groups.get(record.finding).push(record);
    }

    const findings = [];
    for (const [finding, records] of groups) {
      const winnerRound = Math.min(...records.map((r) => r.round));
      const winners = records
        .filter((r) => r.round === winnerRound)
        .sort((a, b) => a.seq - b.seq); // 序号仅决定输出顺序，不决定优先权
      // 承诺隐藏了指纹，无法确认未揭示存证是否针对同一发现；
      // 因此保守地将轮次不晚于获胜轮次的未揭示存证视为潜在更早首报，
      // 阻止暂定结果升级为定案。
      const blockedByEarlierDeposit =
        phase !== PHASE.CLOSED &&
        unrevealed.some((r) => r.round <= winnerRound);
      findings.push({
        finding,
        project: winners[0].project,
        winnerRound,
        blockedByEarlierDeposit,
        status: phase === PHASE.CLOSED
          ? ADJUDICATION_STATUS.FINAL
          : ADJUDICATION_STATUS.PROVISIONAL,
        winners: winners.map((r) => ({
          depositId: r.id,
          seq: r.seq,
          round: r.round,
          reporter: r.reporter,
          revealedAt: r.revealedAt,
        })),
        tied: winners.length > 1,
      });
    }

    findings.sort((a, b) => {
      if (a.winnerRound !== b.winnerRound) return a.winnerRound - b.winnerRound;
      return a.finding.localeCompare(b.finding);
    });

    return {
      status: phase === PHASE.CLOSED
        ? ADJUDICATION_STATUS.FINAL
        : ADJUDICATION_STATUS.PROVISIONAL,
      phase,
      decidedAt: now,
      findings,
      counts: {
        deposits: all.length,
        revealed: all.filter((r) => r.revealed).length,
        eligible: eligible.length,
        // 定案时未揭示存证已被剔除，不计入裁决依据。
        unrevealed: phase === PHASE.CLOSED ? 0 : unrevealed.length,
        purgedUnrevealed: phase === PHASE.CLOSED ? unrevealed.length : 0,
        rejectedReveals: this.#auditLog.filter((e) => e.type !== 'duplicate-reveal').length,
      },
    };
  }

  /**
   * 外部回执：
   * - 已揭示存证输出身份（项目/报告人/发现），但绝不输出盐；
   * - 未揭示存证只保留标识性元数据（id/序号/轮次/摘要/时间），身份字段全部缺席；
   * - 输出按接收序号排序，保证可复现。
   */
  getReceipt() {
    const deposits = [...this.#records.values()]
      .sort((a, b) => a.seq - b.seq)
      .map(toPublicView);
    return {
      generatedAt: this.now(),
      phase: this.phase(),
      depositDeadline: this.#revealOpensAt,
      revealOpensAt: this.#revealOpensAt,
      revealDeadline: this.#revealDeadline,
      deposits,
      counts: {
        deposits: deposits.length,
        revealed: deposits.filter((d) => d.revealed).length,
        unrevealed: deposits.filter((d) => !d.revealed).length,
      },
    };
  }

  /**
   * 内部审计访问器（含被拒绝的揭示尝试）。审计事件不含任何明文或盐。
   * 外部流程应使用 getReceipt()，不得依赖本方法的结构。
   */
  getAuditLog() {
    return this.#auditLog.map((e) => ({ ...e }));
  }

  /**
   * 内部记录访问器（含盐），仅供受控审计与测试使用，禁止用于外部回执。
   */
  getInternalRecord(depositId) {
    const record = this.#records.get(depositId);
    if (!record) throw new UnknownDepositError();
    return { ...record, digest: Buffer.from(record.digest) };
  }

  #audit(type, depositId) {
    this.#auditLog.push({ type, depositId, at: this.now() });
  }
}
