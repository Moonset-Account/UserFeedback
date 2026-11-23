// 把引擎原始结果整理成对外报告：每路计划/实际发出/从未发出/最终完成，
// 以及按 logicalId 的双路配对（重试只是同一条目下的尝试，不产生新配对）。

import { FINAL_STATUS } from "./engine.mjs";

function summarize(raw) {
  const requests = raw.requests.map((r) => ({ ...r }));
  const byStatus = new Map();
  for (const status of Object.values(FINAL_STATUS)) byStatus.set(status, 0);
  for (const request of requests) {
    byStatus.set(request.finalStatus, byStatus.get(request.finalStatus) + 1);
  }

  const sessionStats = new Map();
  for (const request of requests) {
    if (!sessionStats.has(request.sessionId)) {
      sessionStats.set(request.sessionId, {
        sessionId: request.sessionId,
        planned: 0,
        sent: 0,
        neverSent: 0,
        succeeded: 0,
        totalWaitMs: 0,
        maxWaitMs: 0,
        waitSamples: 0,
      });
    }
    const stats = sessionStats.get(request.sessionId);
    stats.planned += 1;
    if (request.attemptCount > 0) {
      stats.sent += 1;
      stats.totalWaitMs += request.waitMs;
      stats.maxWaitMs = Math.max(stats.maxWaitMs, request.waitMs);
      stats.waitSamples += 1;
    } else {
      // 从未发出 = 等到截止仍未获得槽位/前序，按截止时刻计入等待，
      // 这样“同一会话的等待只在一边增加”不会因为样本被跳过而失真。
      stats.neverSent += 1;
      const boundedWait =
        raw.deadlineMs === null ? null : raw.deadlineMs - request.plannedArrival;
      if (boundedWait !== null) {
        stats.totalWaitMs += boundedWait;
        stats.maxWaitMs = Math.max(stats.maxWaitMs, boundedWait);
        stats.waitSamples += 1;
      }
    }
    if (request.finalStatus === FINAL_STATUS.SUCCEEDED) stats.succeeded += 1;
  }
  for (const stats of sessionStats.values()) {
    stats.avgWaitMs = stats.waitSamples > 0 ? stats.totalWaitMs / stats.waitSamples : null;
  }

  return {
    label: raw.label,
    deadlineMs: raw.deadlineMs,
    endedAt: raw.endedAt,
    plannedCount: requests.length,
    sentCount: requests.filter((r) => r.attemptCount > 0).length,
    neverSentCount: byStatus.get(FINAL_STATUS.NEVER_SENT),
    successCount: byStatus.get(FINAL_STATUS.SUCCEEDED),
    failedCount: byStatus.get(FINAL_STATUS.FAILED),
    inFlightCount: byStatus.get(FINAL_STATUS.IN_FLIGHT),
    totalAttempts: requests.reduce((sum, r) => sum + r.attemptCount, 0),
    retryAttempts: requests.reduce((sum, r) => sum + Math.max(0, r.attemptCount - 1), 0),
    byStatus: Object.fromEntries(byStatus),
    requests,
    sessions: [...sessionStats.values()].sort((a, b) =>
      a.sessionId < b.sessionId ? -1 : a.sessionId > b.sessionId ? 1 : 0,
    ),
  };
}

/**
 * 双路配对。配对键是逻辑请求本身（logicalId + round），
 * 额外重试嵌套在 attempts 内，绝不计为另一组原始负载。
 */
function pairReports(baseline, candidate, records) {
  const baselineById = new Map(baseline.requests.map((r) => [r.logicalId, r]));
  const candidateById = new Map(candidate.requests.map((r) => [r.logicalId, r]));

  const pairs = [];
  for (const record of records) {
    const base = baselineById.get(record.logicalId);
    const cand = candidateById.get(record.logicalId);
    if (!base || !cand) {
      throw new Error(`逻辑请求 ${record.logicalId} 在某一路缺失，无法配对`);
    }
    if (base.plannedArrival !== cand.plannedArrival || base.round !== cand.round) {
      throw new Error(
        `逻辑请求 ${record.logicalId} 双路初始到达安排不一致，共享到达安排被破坏`,
      );
    }
    pairs.push({
      logicalId: record.logicalId,
      round: record.round,
      sessionId: record.sessionId,
      predecessor: record.predecessor,
      plannedArrival: record.plannedArrival,
      baseline: {
        finalStatus: base.finalStatus,
        blockingReason: base.blockingReason,
        firstSentAt: base.firstSentAt,
        lastCompletedAt: base.lastCompletedAt,
        waitMs: base.waitMs,
        attemptCount: base.attemptCount,
        attempts: base.attempts,
      },
      candidate: {
        finalStatus: cand.finalStatus,
        blockingReason: cand.blockingReason,
        firstSentAt: cand.firstSentAt,
        lastCompletedAt: cand.lastCompletedAt,
        waitMs: cand.waitMs,
        attemptCount: cand.attemptCount,
        attempts: cand.attempts,
      },
      extraRetries: {
        baseline: Math.max(0, base.attemptCount - 1),
        candidate: Math.max(0, cand.attemptCount - 1),
      },
    });
  }

  return pairs;
}

export function buildReport(rawBaseline, rawCandidate, normalizedRecords) {
  const baseline = summarize(rawBaseline);
  const candidate = summarize(rawCandidate);
  const pairs = pairReports(baseline, candidate, normalizedRecords);

  return {
    sharedArrivalSchedule: normalizedRecords.map((r) => ({
      logicalId: r.logicalId,
      round: r.round,
      plannedArrival: r.plannedArrival,
      sessionId: r.sessionId,
      predecessor: r.predecessor,
    })),
    paths: { baseline, candidate },
    pairing: {
      pairCount: pairs.length,
      pairs,
    },
  };
}
