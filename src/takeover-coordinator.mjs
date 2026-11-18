/**
 * 接管协调模块（端点防护代理升级）
 *
 * 职责：把多个观测源逐个从旧版代理交接给新版代理，保证任一时刻每个
 * 序号都有且仅有一个负责处置方；新版可提前影子接收相同事件，但只有
 * 接管边界之后的序号由新版提交处置；新版断开时回退到旧版实际能补齐
 * 的边界，无法补齐的序号作为观测缺口单独拦截；迟到的旧版回执只补
 * 历史，不改变新版已提交的处置决定。
 */

export class TakeoverError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'TakeoverError';
    this.code = code;
  }
}

export const OWNER = Object.freeze({ OLD: 'old', NEW: 'new' });

class SourceState {
  constructor(id, firstSeq) {
    this.id = id;
    this.firstSeq = firstSeq;
    this.owner = OWNER.OLD;          // 当前负责处置方
    this.newReady = false;           // 新版准备确认
    this.shadowing = false;          // 新版影子接收（提前接收相同事件）
    this.oldWatermark = firstSeq - 1; // 旧版消费水位（已连续消费到的序号）
    this.oldBackfillLimit = firstSeq - 1; // 旧版实际能补齐到的序号
    this.takeoverBoundary = null;    // 接管边界：>= 边界的序号由新版提交
    this.lastSeq = firstSeq - 1;     // 已接收到的最大连续/非连续序号
    this.commits = new Map();        // seq -> { owner, disposition, dispositionBy }
    this.gaps = [];                  // 观测缺口 { from, to, reason }
    this.history = [];               // 迟到回执/补录历史（不改变已提交决定）
  }
}

export class TakeoverCoordinator {
  #sources = new Map();

  /** 注册观测源，声明其连续序号起点。 */
  registerSource(id, { firstSeq = 1 } = {}) {
    if (this.#sources.has(id)) {
      throw new TakeoverError('DUP_SOURCE', `source already registered: ${id}`);
    }
    if (!Number.isInteger(firstSeq) || firstSeq < 0) {
      throw new TakeoverError('BAD_FIRST_SEQ', `invalid firstSeq: ${firstSeq}`);
    }
    this.#sources.set(id, new SourceState(id, firstSeq));
    return this.snapshot(id);
  }

  #state(id) {
    const s = this.#sources.get(id);
    if (!s) throw new TakeoverError('UNKNOWN_SOURCE', `unknown source: ${id}`);
    return s;
  }

  /**
   * 接入一个源事件。按当前归属提交给负责方；影子期新版同步预览但不提交。
   * 序号跳变时，缺失区间作为观测缺口单独拦截（不影响本事件的接收）。
   */
  ingest(id, seq, payload = null) {
    const s = this.#state(id);
    if (!Number.isInteger(seq) || seq < s.firstSeq) {
      throw new TakeoverError('BAD_SEQ', `invalid seq ${seq} for source ${id}`);
    }
    if (s.commits.has(seq)) {
      throw new TakeoverError('DUP_SEQ', `duplicate seq ${seq} for source ${id}`);
    }
    if (seq <= s.lastSeq) {
      throw new TakeoverError('SEQ_REGRESSION', `seq ${seq} not after ${s.lastSeq}`);
    }
    if (seq > s.lastSeq + 1) {
      s.gaps.push({ from: s.lastSeq + 1, to: seq - 1, reason: 'ingest-discontinuity' });
    }
    s.lastSeq = seq;

    const committedTo =
      s.owner === OWNER.NEW && seq >= s.takeoverBoundary ? OWNER.NEW : OWNER.OLD;
    s.commits.set(seq, {
      owner: committedTo,
      disposition: null,
      dispositionBy: null,
      payload,
    });
    const previewedBy = s.shadowing && committedTo === OWNER.OLD ? [OWNER.NEW] : [];
    return { source: id, seq, committedTo, previewedBy };
  }

  /** 旧版消费水位上报（只前进）。 */
  reportOldWatermark(id, seq) {
    const s = this.#state(id);
    if (!Number.isInteger(seq) || seq < s.oldWatermark) {
      throw new TakeoverError('WATERMARK_REGRESSION', `watermark ${seq} before ${s.oldWatermark}`);
    }
    if (seq > s.lastSeq) {
      throw new TakeoverError('WATERMARK_AHEAD', `watermark ${seq} beyond last seq ${s.lastSeq}`);
    }
    s.oldWatermark = seq;
    return this.snapshot(id);
  }

  /** 旧版实际能补齐到的序号（断开回退时的能力边界，只前进）。 */
  reportOldBackfillLimit(id, seq) {
    const s = this.#state(id);
    if (!Number.isInteger(seq) || seq < s.oldBackfillLimit) {
      throw new TakeoverError('BACKFILL_REGRESSION', `backfill ${seq} before ${s.oldBackfillLimit}`);
    }
    if (seq > s.lastSeq) {
      throw new TakeoverError('BACKFILL_AHEAD', `backfill ${seq} beyond last seq ${s.lastSeq}`);
    }
    s.oldBackfillLimit = seq;
    return this.snapshot(id);
  }

  /** 新版准备确认：进入影子接收，可提前收到相同事件但不提交处置。 */
  confirmNewReady(id) {
    const s = this.#state(id);
    if (s.owner === OWNER.NEW) {
      throw new TakeoverError('ALREADY_TAKEN_OVER', `source ${id} already owned by new`);
    }
    s.newReady = true;
    s.shadowing = true;
    return this.snapshot(id);
  }

  /**
   * 可控升级事件：提交接管。
   * 交叉约束：新版必须已就绪；边界不得越过旧版消费水位+1（否则产生缺口，拒绝）。
   */
  commitTakeover(id, boundary) {
    const s = this.#state(id);
    if (!s.newReady) {
      throw new TakeoverError('NEW_NOT_READY', `new version not ready for ${id}`);
    }
    if (s.owner === OWNER.NEW) {
      throw new TakeoverError('ALREADY_TAKEN_OVER', `source ${id} already owned by new`);
    }
    if (!Number.isInteger(boundary) || boundary < s.firstSeq) {
      throw new TakeoverError('BAD_BOUNDARY', `invalid boundary ${boundary}`);
    }
    if (boundary > s.oldWatermark + 1) {
      throw new TakeoverError(
        'BOUNDARY_GAP',
        `boundary ${boundary} exceeds old watermark ${s.oldWatermark} + 1`,
      );
    }
    s.owner = OWNER.NEW;
    s.takeoverBoundary = boundary;
    s.shadowing = false;
    return this.snapshot(id);
  }

  /**
   * 提交处置决定。
   * - by=new：仅对接管边界后、归属新版的序号有效。
   * - by=old：旧版回执。若序号已由新版提交处置，则只记入历史（迟到回执
   *   补历史，不改变新版决定）；若归属旧版且尚无处置，则补录处置。
   */
  submitDisposition(id, seq, { by, decision }) {
    const s = this.#state(id);
    const commit = s.commits.get(seq);
    if (!commit) {
      s.history.push({ seq, by, decision, note: 'no-commit-recorded' });
      return { source: id, seq, applied: false, recordedAs: 'history' };
    }
    if (by === OWNER.NEW) {
      if (commit.owner !== OWNER.NEW) {
        throw new TakeoverError('NOT_OWNER', `seq ${seq} not owned by new version`);
      }
      if (commit.disposition !== null) {
        throw new TakeoverError('DUP_DISPOSITION', `seq ${seq} already dispositioned`);
      }
      commit.disposition = decision;
      commit.dispositionBy = OWNER.NEW;
      return { source: id, seq, applied: true, owner: OWNER.NEW };
    }
    if (by === OWNER.OLD) {
      if (commit.owner === OWNER.NEW || commit.disposition !== null) {
        // 迟到/重复的旧版回执：补历史，不改变已提交的处置决定。
        s.history.push({ seq, by, decision, note: 'superseded-or-duplicate' });
        return { source: id, seq, applied: false, recordedAs: 'history' };
      }
      commit.disposition = decision;
      commit.dispositionBy = OWNER.OLD;
      return { source: id, seq, applied: true, owner: OWNER.OLD };
    }
    throw new TakeoverError('BAD_ACTOR', `unknown disposer: ${by}`);
  }

  /**
   * 新版断开：回退到旧版实际能补齐的边界。
   * - 新版已提交处置的序号：决定保持不变；
   * - 归属新版但未处置、且旧版能补齐的序号：重新划归旧版；
   * - 旧版也补不齐的序号：作为观测缺口单独拦截（不再归属任何一方）。
   */
  reportNewDisconnect(id) {
    const s = this.#state(id);
    if (s.owner !== OWNER.NEW) {
      throw new TakeoverError('NOT_TAKEN_OVER', `source ${id} is not owned by new`);
    }
    const reassigned = [];
    const lost = [];
    for (const [seq, commit] of [...s.commits.entries()].sort((a, b) => a[0] - b[0])) {
      if (commit.owner !== OWNER.NEW || commit.disposition !== null) continue;
      if (seq <= s.oldBackfillLimit) {
        commit.owner = OWNER.OLD;
        reassigned.push(seq);
      } else {
        s.commits.delete(seq);
        lost.push(seq);
      }
    }
    for (const { from, to } of toRanges(lost)) {
      s.gaps.push({ from, to, reason: 'new-disconnect-unrecoverable' });
    }
    const fallbackBoundary = s.oldBackfillLimit + 1;
    s.owner = OWNER.OLD;
    s.newReady = false;
    s.shadowing = false;
    s.takeoverBoundary = null;
    return { source: id, fallbackBoundary, reassigned, gaps: toRanges(lost) };
  }

  /** 覆盖性核验：[firstSeq, lastSeq] 内每个序号要么有归属提交，要么被缺口拦截。 */
  verifyCoverage(id) {
    const s = this.#state(id);
    const uncovered = [];
    for (let seq = s.firstSeq; seq <= s.lastSeq; seq += 1) {
      if (!s.commits.has(seq) && !inAnyGap(s.gaps, seq)) uncovered.push(seq);
    }
    return { source: id, ok: uncovered.length === 0, uncovered };
  }

  snapshot(id) {
    const s = this.#state(id);
    return {
      source: s.id,
      owner: s.owner,
      newReady: s.newReady,
      shadowing: s.shadowing,
      oldWatermark: s.oldWatermark,
      oldBackfillLimit: s.oldBackfillLimit,
      takeoverBoundary: s.takeoverBoundary,
      lastSeq: s.lastSeq,
      commits: [...s.commits.entries()]
        .sort((a, b) => a[0] - b[0])
        .map(([seq, c]) => ({
          seq,
          owner: c.owner,
          disposition: c.disposition,
          dispositionBy: c.dispositionBy,
        })),
      gaps: s.gaps.map((g) => ({ ...g })),
      history: s.history.map((h) => ({ ...h })),
    };
  }
}

function toRanges(sortedSeqs) {
  const ranges = [];
  for (const seq of sortedSeqs) {
    const last = ranges[ranges.length - 1];
    if (last && seq === last.to + 1) last.to = seq;
    else ranges.push({ from: seq, to: seq });
  }
  return ranges;
}

function inAnyGap(gaps, seq) {
  return gaps.some((g) => seq >= g.from && seq <= g.to);
}
