// 题面场景：基线有一个请求挂起不返回，候选快速结束。
// 同一会话 A 的等待只在基线一侧累积；会话 B 在两条线上逐条对齐、未被拖延。
// 运行：node examples/headline-scenario.mjs

import { runDualPathReplay } from "../src/index.mjs";

const records = [
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

const DEADLINE = 1000;

const { report, explanation } = runDualPathReplay(records, {
  baseline: {
    sessionConcurrency: 1,
    deadlineMs: DEADLINE,
    outcome(logicalId, attempt) {
      if (logicalId === "A1") return { status: "hang" };
      if (logicalId === "A2" && attempt === 1) {
        return { status: "failure", latencyMs: 20, retryable: true };
      }
      if (logicalId === "B1") return { status: "success", latencyMs: 100 };
      if (logicalId === "B2") return { status: "success", latencyMs: 100 };
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
});

console.log("=== 共享初始到达安排（逐条对齐）===");
for (const entry of report.sharedArrivalSchedule) {
  console.log(
    `  ${entry.logicalId} round=${entry.round} t=${entry.plannedArrival} ` +
      `session=${entry.sessionId} after=${entry.predecessor ?? "-"}`,
  );
}

for (const side of ["baseline", "candidate"]) {
  const path = report.paths[side];
  console.log(`\n=== ${side} 路 ===`);
  console.log(
    `  计划=${path.plannedCount} 发出=${path.sentCount} 从未发出=${path.neverSentCount} ` +
      `成功=${path.successCount} 在途(挂起)=${path.inFlightCount} ` +
      `总尝试=${path.totalAttempts} 其中重试=${path.retryAttempts}`,
  );
  for (const request of path.requests) {
    const sent =
      request.firstSentAt === null
        ? "从未发出"
        : `首发t=${request.firstSentAt} 等待=${request.waitMs}ms 尝试=${request.attemptCount}`;
    console.log(`  ${request.logicalId}: ${request.finalStatus} ${sent}`);
  }
}

console.log("\n=== 双路配对（按逻辑 ID，重试不另成负载）===");
for (const pair of report.pairing.pairs) {
  console.log(
    `  ${pair.logicalId}: 基线[${pair.baseline.finalStatus}, 尝试${pair.baseline.attemptCount}] ` +
      `候选[${pair.candidate.finalStatus}, 尝试${pair.candidate.attemptCount}] ` +
      `额外重试 基线=${pair.extraRetries.baseline} 候选=${pair.extraRetries.candidate}`,
  );
}

console.log("\n=== 会话等待解释 ===");
for (const line of explanation.narrative) console.log(`  ${line}`);
