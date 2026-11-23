import { test, describe } from "node:test";
import assert from "node:assert/strict";

import { runDualPathReplay, runSinglePath } from "../src/index.mjs";
import { buildReport } from "../src/report.mjs";
import { normalizeRecords } from "../src/records.mjs";
import { FINAL_STATUS } from "../src/engine.mjs";

const DEADLINE = 1000;

function headlineRecords() {
  return [
    {
      logicalId: "A1",
      round: 1,
      plannedArrival: 0,
      sessionId: "session-A",
      predecessor: null,
      retryPolicy: { maxAttempts: 3, backoffMs: 50 },
    },
    {
      logicalId: "A2",
      round: 1,
      plannedArrival: 100,
      sessionId: "session-A",
      predecessor: "A1",
      retryPolicy: { maxAttempts: 3, backoffMs: 50 },
    },
    {
      logicalId: "A3",
      round: 1,
      plannedArrival: 200,
      sessionId: "session-A",
      predecessor: "A2",
      retryPolicy: { maxAttempts: 2, backoffMs: 50 },
    },
    {
      logicalId: "B1",
      round: 1,
      plannedArrival: 150,
      sessionId: "session-B",
      predecessor: null,
      retryPolicy: { maxAttempts: 2, backoffMs: 25 },
    },
    {
      logicalId: "B2",
      round: 1,
      plannedArrival: 300,
      sessionId: "session-B",
      predecessor: "B1",
      retryPolicy: { maxAttempts: 2, backoffMs: 25 },
    },
  ];
}

function headlineConfigs() {
  return {
    baseline: {
      sessionConcurrency: 1,
      deadlineMs: DEADLINE,
      outcome(logicalId, attempt) {
        if (logicalId === "A1") return { status: "hang" };
        if (logicalId === "A2" && attempt === 1) {
          return { status: "failure", latencyMs: 20, retryable: true };
        }
        if (logicalId === "B1" || logicalId === "B2") {
          return { status: "success", latencyMs: 100 };
        }
        return { status: "success", latencyMs: 30 };
      },
    },
    candidate: {
      sessionConcurrency: 1,
      deadlineMs: DEADLINE,
      outcome(logicalId, attempt) {
        if (logicalId === "A2" && attempt === 1) {
          return { status: "failure", latencyMs: 20, retryable: true };
        }
        return { status: "success", latencyMs: 100 };
      },
    },
  };
}

describe("题面验收：基线挂起、候选快速结束", () => {
  test("两路拿到同一份初始到达安排并逐条对齐", () => {
    const { report } = runDualPathReplay(headlineRecords(), headlineConfigs());
    assert.equal(report.sharedArrivalSchedule.length, 5);
    for (const pair of report.pairing.pairs) {
      const scheduled = report.sharedArrivalSchedule.find(
        (entry) => entry.logicalId === pair.logicalId,
      );
      assert.equal(pair.plannedArrival, scheduled.plannedArrival);
      assert.equal(pair.round, scheduled.round);
      // 实际首发都不早于共享的计划到达。
      if (pair.baseline.firstSentAt !== null) {
        assert.ok(pair.baseline.firstSentAt >= pair.plannedArrival);
      }
      if (pair.candidate.firstSentAt !== null) {
        assert.ok(pair.candidate.firstSentAt >= pair.plannedArrival);
      }
    }
  });

  test("基线：A1 在途挂起，A2/A3 从未发出；会话 B 全部成功", () => {
    const { report } = runDualPathReplay(headlineRecords(), headlineConfigs());
    const baseline = new Map(
      report.paths.baseline.requests.map((r) => [r.logicalId, r]),
    );
    assert.equal(baseline.get("A1").finalStatus, FINAL_STATUS.IN_FLIGHT);
    assert.equal(baseline.get("A1").attemptCount, 1);
    assert.equal(baseline.get("A2").finalStatus, FINAL_STATUS.NEVER_SENT);
    assert.equal(baseline.get("A2").blockingReason, "dependency");
    assert.equal(baseline.get("A3").finalStatus, FINAL_STATUS.NEVER_SENT);
    assert.equal(baseline.get("A3").blockingReason, "dependency");
    assert.equal(baseline.get("B1").finalStatus, FINAL_STATUS.SUCCEEDED);
    assert.equal(baseline.get("B2").finalStatus, FINAL_STATUS.SUCCEEDED);

    assert.equal(report.paths.baseline.plannedCount, 5);
    assert.equal(report.paths.baseline.sentCount, 3);
    assert.equal(report.paths.baseline.neverSentCount, 2);
    assert.equal(report.paths.baseline.inFlightCount, 1);
    assert.equal(report.paths.baseline.successCount, 2);
  });

  test("候选：全部成功，A2 的失败在本路独立重试后恢复", () => {
    const { report } = runDualPathReplay(headlineRecords(), headlineConfigs());
    const candidate = new Map(
      report.paths.candidate.requests.map((r) => [r.logicalId, r]),
    );
    for (const id of ["A1", "A2", "A3", "B1", "B2"]) {
      assert.equal(candidate.get(id).finalStatus, FINAL_STATUS.SUCCEEDED);
    }
    const a2 = candidate.get("A2");
    assert.equal(a2.attemptCount, 2);
    assert.equal(a2.attempts[0].outcome.status, "failure");
    assert.equal(a2.attempts[1].outcome.status, "success");
    assert.equal(a2.attempts[1].sentAt, a2.attempts[0].completedAt + 50);
    // 时间线：A1 [0,100]，A2 首发100失败120，重试170成功270，A3 270->370。
    assert.equal(a2.firstSentAt, 100);
    assert.equal(candidate.get("A3").firstSentAt, 270);
  });

  test("配对只按逻辑请求：5 个配对，重试不另成一组负载，也不只保留双成功", () => {
    const { report } = runDualPathReplay(headlineRecords(), headlineConfigs());
    assert.equal(report.pairing.pairCount, 5);
    const pairedIds = report.pairing.pairs.map((pair) => pair.logicalId).sort();
    assert.deepEqual(pairedIds, ["A1", "A2", "A3", "B1", "B2"]);

    // 即使基线侧从未成功，配对仍然存在（不是 inner join 只剩双成功）。
    const a2Pair = report.pairing.pairs.find((pair) => pair.logicalId === "A2");
    assert.equal(a2Pair.baseline.finalStatus, FINAL_STATUS.NEVER_SENT);
    assert.equal(a2Pair.candidate.finalStatus, FINAL_STATUS.SUCCEEDED);
    assert.equal(a2Pair.candidate.attemptCount, 2);
    assert.equal(a2Pair.extraRetries.candidate, 1);
    assert.equal(a2Pair.extraRetries.baseline, 0);
  });

  test("会话 A 的等待只在基线一侧增加，会话 B 双路逐条对齐未被拖延", () => {
    const { report, explanation } = runDualPathReplay(
      headlineRecords(),
      headlineConfigs(),
    );
    const bySession = new Map(
      explanation.sessionComparison.map((session) => [session.sessionId, session]),
    );

    const sessionA = bySession.get("session-A");
    assert.equal(sessionA.unaffected, false);
    assert.ok(sessionA.maxWaitDeltaMs > 0);
    // 候选侧 A 链全部按依赖正常推进；基线侧 A2/A3 根本没发出。
    const a2 = sessionA.perRequest.find((entry) => entry.logicalId === "A2");
    assert.equal(a2.baselineFirstSentAt, null);
    assert.equal(a2.candidateFirstSentAt, 100);

    const sessionB = bySession.get("session-B");
    assert.equal(sessionB.unaffected, true);
    assert.equal(sessionB.maxWaitDeltaMs, 0);
    for (const entry of sessionB.perRequest) {
      assert.equal(entry.baselineFirstSentAt, entry.candidateFirstSentAt);
      assert.notEqual(entry.baselineFirstSentAt, null);
    }

    assert.deepEqual(explanation.delayedSessions, ["session-A"]);
    assert.deepEqual(explanation.unaffectedSessions, ["session-B"]);
    assert.deepEqual(
      explanation.hung.baseline.map((item) => item.logicalId),
      ["A1"],
    );
    assert.deepEqual(explanation.hung.candidate, []);
    assert.ok(
      explanation.narrative.some((line) => line.includes("session-A")),
      "解释文本应点名被拖延的会话 A",
    );
    assert.ok(
      explanation.narrative.some((line) => line.includes("session-B") && line.includes("未被拖延")),
      "解释文本应说明会话 B 未被拖延",
    );
  });
});

describe("路间隔离：一方快不会提前另一方的初始负载", () => {
  test("候选瞬间完成时，基线的请求仍按自己的到达时刻与依赖发出", () => {
    const records = [
      {
        logicalId: "X1",
        plannedArrival: 100,
        sessionId: "x",
        predecessor: null,
        retryPolicy: { maxAttempts: 1 },
      },
      {
        logicalId: "X2",
        plannedArrival: 200,
        sessionId: "x",
        predecessor: "X1",
        retryPolicy: { maxAttempts: 1 },
      },
    ];
    const { report } = runDualPathReplay(records, {
      baseline: {
        sessionConcurrency: 1,
        outcome: () => ({ status: "success", latencyMs: 50 }),
      },
      candidate: {
        sessionConcurrency: 1,
        outcome: () => ({ status: "success", latencyMs: 0 }),
      },
    });
    const baseline = new Map(
      report.paths.baseline.requests.map((r) => [r.logicalId, r]),
    );
    const candidate = new Map(
      report.paths.candidate.requests.map((r) => [r.logicalId, r]),
    );
    // 基线不被候选的零延迟“提速”：X1 在 t=100 发出、150 完成；
    // X2 虽无依赖阻塞，但计划到达是 200，到达门控让它最早 200 才能发出。
    assert.equal(baseline.get("X1").firstSentAt, 100);
    assert.equal(baseline.get("X2").firstSentAt, 200);
    // 候选同样不早于自己的计划到达。
    assert.equal(candidate.get("X1").firstSentAt, 100);
    assert.equal(candidate.get("X2").firstSentAt, 200);
  });

  test("重试只在发生失败的那一路出现，配对的额外重试计数不对称", () => {
    const records = [
      {
        logicalId: "F1",
        plannedArrival: 0,
        sessionId: "f",
        predecessor: null,
        retryPolicy: { maxAttempts: 3, backoffMs: 10 },
      },
    ];
    const { report } = runDualPathReplay(records, {
      baseline: {
        sessionConcurrency: 1,
        outcome: (id, attempt) =>
          attempt < 3
            ? { status: "failure", latencyMs: 5, retryable: true }
            : { status: "success", latencyMs: 5 },
      },
      candidate: {
        sessionConcurrency: 1,
        outcome: () => ({ status: "success", latencyMs: 5 }),
      },
    });
    const pair = report.pairing.pairs[0];
    assert.equal(pair.baseline.attemptCount, 3);
    assert.equal(pair.candidate.attemptCount, 1);
    assert.equal(pair.extraRetries.baseline, 2);
    assert.equal(pair.extraRetries.candidate, 0);
    // 仍然只有一个逻辑请求配对，重试没有膨胀为额外配对。
    assert.equal(report.pairing.pairCount, 1);
  });
});

describe("故障恢复验收：失败-退避-恢复的交错时间线", () => {
  test("两路各自独立重试，最终在同逻辑 ID 下收敛", () => {
    const records = [
      {
        logicalId: "R1",
        plannedArrival: 0,
        sessionId: "r",
        predecessor: null,
        retryPolicy: { maxAttempts: 4, backoffMs: 20 },
      },
      {
        logicalId: "R2",
        plannedArrival: 0,
        sessionId: "r",
        predecessor: "R1",
        retryPolicy: { maxAttempts: 2, backoffMs: 10 },
      },
    ];
    const { report } = runDualPathReplay(records, {
      baseline: {
        sessionConcurrency: 1,
        outcome: (id, attempt) =>
          id === "R1" && attempt <= 2
            ? { status: "failure", latencyMs: 10, retryable: true }
            : { status: "success", latencyMs: 10 },
      },
      candidate: {
        sessionConcurrency: 1,
        outcome: (id, attempt) =>
          id === "R1" && attempt === 1
            ? { status: "failure", latencyMs: 10, retryable: true }
            : { status: "success", latencyMs: 10 },
      },
    });
    const r1 = report.pairing.pairs.find((pair) => pair.logicalId === "R1");
    const r2 = report.pairing.pairs.find((pair) => pair.logicalId === "R2");

    // 基线：失败10 -> 30 失败40 -> 60 成功70；候选：失败10 -> 30 成功40。
    assert.deepEqual(
      r1.baseline.attempts.map((a) => a.sentAt),
      [0, 30, 60],
    );
    assert.deepEqual(
      r1.candidate.attempts.map((a) => a.sentAt),
      [0, 30],
    );
    assert.equal(r1.baseline.finalStatus, FINAL_STATUS.SUCCEEDED);
    assert.equal(r1.candidate.finalStatus, FINAL_STATUS.SUCCEEDED);

    // 后继各自等本路 R1 真正成功后才发出，两条线时间不同。
    assert.equal(r2.baseline.firstSentAt, 70);
    assert.equal(r2.candidate.firstSentAt, 40);
    assert.equal(r2.baseline.finalStatus, FINAL_STATUS.SUCCEEDED);
  });

  test("一路彻底失败、另一路恢复时，配对仍保留失败侧而不是剔除", () => {
    const records = [
      {
        logicalId: "R1",
        plannedArrival: 0,
        sessionId: "r",
        predecessor: null,
        retryPolicy: { maxAttempts: 2, backoffMs: 10 },
      },
    ];
    const { report } = runDualPathReplay(records, {
      baseline: {
        sessionConcurrency: 1,
        outcome: () => ({ status: "failure", latencyMs: 5, retryable: true }),
      },
      candidate: {
        sessionConcurrency: 1,
        outcome: (attempt) =>
          attempt === 1
            ? { status: "failure", latencyMs: 5, retryable: true }
            : { status: "success", latencyMs: 5 },
      },
    });
    const pair = report.pairing.pairs[0];
    assert.equal(pair.baseline.finalStatus, FINAL_STATUS.FAILED);
    assert.equal(pair.candidate.finalStatus, FINAL_STATUS.SUCCEEDED);
    assert.equal(report.paths.baseline.failedCount, 1);
    assert.equal(report.paths.candidate.successCount, 1);
  });
});

describe("多轮到达的交错场景", () => {
  test("第二轮初始负载在两路都按计划加入，配对以逻辑 ID 为准而非轮次计数", () => {
    const records = [
      {
        logicalId: "W1-R1",
        round: 1,
        plannedArrival: 0,
        sessionId: "w1",
        predecessor: null,
        retryPolicy: { maxAttempts: 1 },
      },
      {
        logicalId: "W2-R1",
        round: 2,
        plannedArrival: 500,
        sessionId: "w2",
        predecessor: null,
        retryPolicy: { maxAttempts: 1 },
      },
      {
        logicalId: "W2-R2",
        round: 2,
        plannedArrival: 500,
        sessionId: "w2",
        predecessor: "W2-R1",
        retryPolicy: { maxAttempts: 1 },
      },
    ];
    const { report } = runDualPathReplay(records, {
      baseline: {
        sessionConcurrency: 1,
        deadlineMs: 1000,
        outcome: (id) =>
          id === "W2-R1" ? { status: "hang" } : { status: "success", latencyMs: 20 },
      },
      candidate: {
        sessionConcurrency: 1,
        deadlineMs: 1000,
        outcome: () => ({ status: "success", latencyMs: 20 }),
      },
    });

    assert.equal(report.pairing.pairCount, 3);
    const w2r1 = report.pairing.pairs.find((pair) => pair.logicalId === "W2-R1");
    assert.equal(w2r1.round, 2);
    assert.equal(w2r1.baseline.finalStatus, FINAL_STATUS.IN_FLIGHT);
    assert.equal(w2r1.candidate.finalStatus, FINAL_STATUS.SUCCEEDED);

    const w2r2 = report.pairing.pairs.find((pair) => pair.logicalId === "W2-R2");
    assert.equal(w2r2.baseline.finalStatus, FINAL_STATUS.NEVER_SENT);
    assert.equal(w2r2.candidate.finalStatus, FINAL_STATUS.SUCCEEDED);
    // 第二轮到达时刻两路一致，没有因为第一轮的快慢而被提前。
    assert.equal(w2r1.plannedArrival, 500);
    assert.equal(w2r1.baseline.firstSentAt, 500);
    assert.equal(w2r1.candidate.firstSentAt, 500);
  });
});

describe("共享安排被破坏时直接报错", () => {
  test("配对阶段校验双路到达安排一致性", () => {
    const records = [
      {
        logicalId: "S1",
        plannedArrival: 0,
        sessionId: "s",
        predecessor: null,
        retryPolicy: { maxAttempts: 1 },
      },
    ];
    // 正常情况下两条路共享同一份规范化记录，这里通过构造验证防御性校验存在。
    const normalized = normalizeRecords(records);
    const raw1 = runSinglePath(
      records,
      { sessionConcurrency: 1, outcome: () => ({ status: "success", latencyMs: 5 }) },
      "baseline",
    );
    const tampered = normalized.map((record) => ({ ...record, plannedArrival: 999 }));
    const raw2 = runSinglePath(
      tampered,
      { sessionConcurrency: 1, outcome: () => ({ status: "success", latencyMs: 5 }) },
      "candidate",
    );
    assert.throws(
      () => buildReport(raw1, raw2, normalized),
      /初始到达安排不一致/,
    );
  });
});
