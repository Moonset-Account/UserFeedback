// 回放解释：回答两个问题——
// 1. 同一会话的等待为何只在一边增加（挂起/重试如何沿会话依赖与并发槽位传导）；
// 2. 哪个会话完全没有被拖延（对照证明影响被隔离在会话边界内）。

import { FINAL_STATUS } from "./engine.mjs";

function findHung(pairs, side) {
  return pairs
    .filter((pair) => pair[side].attempts.some((a) => a.outcome.status === "hang"))
    .map((pair) => ({ logicalId: pair.logicalId, sessionId: pair.sessionId }));
}

function findRetries(pairs, side) {
  return pairs
    .filter((pair) => pair[side].attemptCount > 1)
    .map((pair) => ({
      logicalId: pair.logicalId,
      sessionId: pair.sessionId,
      attempts: pair[side].attemptCount,
    }));
}

function compareSessions(report) {
  const { baseline, candidate } = report.paths;
  const baseSessions = new Map(baseline.sessions.map((s) => [s.sessionId, s]));
  const candSessions = new Map(candidate.sessions.map((s) => [s.sessionId, s]));

  const results = [];
  for (const sessionId of [...baseSessions.keys()].sort()) {
    const base = baseSessions.get(sessionId);
    const cand = candSessions.get(sessionId);
    if (!cand) throw new Error(`会话 ${sessionId} 在候选路缺失`);

    const perRequest = report.pairing.pairs
      .filter((pair) => pair.sessionId === sessionId)
      .map((pair) => ({
        logicalId: pair.logicalId,
        baselineFirstSentAt: pair.baseline.firstSentAt,
        candidateFirstSentAt: pair.candidate.firstSentAt,
        baselineWaitMs: pair.baseline.waitMs,
        candidateWaitMs: pair.candidate.waitMs,
        baselineStatus: pair.baseline.finalStatus,
        candidateStatus: pair.candidate.finalStatus,
        baselineBlockingReason: pair.baseline.blockingReason,
        candidateBlockingReason: pair.candidate.blockingReason,
        firstSentDeltaMs:
          pair.baseline.firstSentAt !== null && pair.candidate.firstSentAt !== null
            ? pair.baseline.firstSentAt - pair.candidate.firstSentAt
            : null,
      }));

    // “未被拖延”：两边每个已发出请求的初次发出时刻完全一致。
    const unaffected = perRequest.every(
      (entry) =>
        entry.baselineFirstSentAt !== null &&
        entry.candidateFirstSentAt !== null &&
        entry.baselineFirstSentAt === entry.candidateFirstSentAt,
    );

    results.push({
      sessionId,
      baseline: {
        maxWaitMs: base.maxWaitMs,
        avgWaitMs: base.avgWaitMs,
        neverSent: base.neverSent,
      },
      candidate: {
        maxWaitMs: cand.maxWaitMs,
        avgWaitMs: cand.avgWaitMs,
        neverSent: cand.neverSent,
      },
      maxWaitDeltaMs: base.maxWaitMs - cand.maxWaitMs,
      unaffected,
      perRequest,
    });
  }
  return results;
}

function buildNarrative(report, sessionComparison, hung, retried) {
  const lines = [];
  lines.push(
    `共享初始到达安排共 ${report.sharedArrivalSchedule.length} 个逻辑请求，` +
      `基线与候选各自独立派发，互不借用对方的时钟或完成事件。`,
  );

  if (hung.baseline.length > 0 || hung.candidate.length > 0) {
    for (const item of hung.baseline) {
      lines.push(
        `基线路请求 ${item.logicalId}（会话 ${item.sessionId}）挂起不返回，` +
          `持续占用该会话的并发槽位；同会话后续请求只能等待，等待在基线一侧累积。`,
      );
    }
    for (const item of hung.candidate) {
      lines.push(
        `候选路请求 ${item.logicalId}（会话 ${item.sessionId}）挂起不返回，` +
          `同会话后续请求在候选一侧等待。`,
      );
    }
  }

  for (const item of retried.baseline) {
    lines.push(
      `基线路请求 ${item.logicalId} 共发出 ${item.attempts} 次尝试，` +
        `重试在基线路独立发生，仍归属逻辑请求 ${item.logicalId}，不形成新的原始负载。`,
    );
  }
  for (const item of retried.candidate) {
    lines.push(
      `候选路请求 ${item.logicalId} 共发出 ${item.attempts} 次尝试，重试独立归属原逻辑请求。`,
    );
  }

  for (const session of sessionComparison) {
    if (session.unaffected && session.maxWaitDeltaMs === 0) {
      lines.push(
        `会话 ${session.sessionId} 未被拖延：双路每个请求的初次发出时刻逐条相同，` +
          `说明一边的慢响应没有越过会话边界。`,
      );
    } else if (session.maxWaitDeltaMs > 0) {
      lines.push(
        `会话 ${session.sessionId} 的等待只在基线一侧增加：` +
          `最大等待基线 ${session.baseline.maxWaitMs}ms 对候选 ${session.candidate.maxWaitMs}ms，` +
          `差值 ${session.maxWaitDeltaMs}ms；候选快速完成后立即释放槽位，不被基线的慢路提前或拖累。`,
      );
    } else if (session.maxWaitDeltaMs < 0) {
      lines.push(
        `会话 ${session.sessionId} 的等待只在候选一侧增加，差值 ${session.maxWaitDeltaMs}ms。`,
      );
    }
  }

  const neverSent = report.pairing.pairs.filter(
    (pair) =>
      pair.baseline.finalStatus === FINAL_STATUS.NEVER_SENT ||
      pair.candidate.finalStatus === FINAL_STATUS.NEVER_SENT,
  );
  for (const pair of neverSent) {
    for (const side of ["baseline", "candidate"]) {
      if (pair[side].finalStatus === FINAL_STATUS.NEVER_SENT) {
        lines.push(
          `${side === "baseline" ? "基线" : "候选"}路请求 ${pair.logicalId} 从未发出，` +
            `原因: ${pair[side].blockingReason}。`,
        );
      }
    }
  }

  return lines;
}

export function explain(report) {
  const sessionComparison = compareSessions(report);
  const hung = {
    baseline: findHung(report.pairing.pairs, "baseline"),
    candidate: findHung(report.pairing.pairs, "candidate"),
  };
  const retried = {
    baseline: findRetries(report.pairing.pairs, "baseline"),
    candidate: findRetries(report.pairing.pairs, "candidate"),
  };

  const delayedSessions = sessionComparison
    .filter((session) => !session.unaffected)
    .map((session) => session.sessionId);
  const unaffectedSessions = sessionComparison
    .filter((session) => session.unaffected)
    .map((session) => session.sessionId);

  return {
    hung,
    retried,
    sessionComparison,
    delayedSessions,
    unaffectedSessions,
    narrative: buildNarrative(report, sessionComparison, hung, retried),
  };
}
