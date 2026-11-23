import { test, describe } from "node:test";
import assert from "node:assert/strict";

import { runSinglePath } from "../src/index.mjs";
import { FINAL_STATUS } from "../src/engine.mjs";
import { MinHeap } from "../src/priority-queue.mjs";

const baseRecord = (overrides) => ({
  logicalId: "X",
  plannedArrival: 0,
  sessionId: "s",
  predecessor: null,
  retryPolicy: { maxAttempts: 1, backoffMs: 0 },
  ...overrides,
});

const successPath = (latencyMs = 10, extra = {}) => ({
  sessionConcurrency: 1,
  outcome: () => ({ status: "success", latencyMs }),
  ...extra,
});

describe("到达门控：未到计划时刻不得提前发出", () => {
  test("初次发出时刻不会早于计划到达时刻", () => {
    const records = [
      baseRecord({ logicalId: "R1", plannedArrival: 100 }),
      baseRecord({ logicalId: "R2", plannedArrival: 250, sessionId: "s2" }),
    ];
    const raw = runSinglePath(records, successPath(10), "p");
    const byId = new Map(raw.requests.map((r) => [r.logicalId, r]));
    assert.equal(byId.get("R1").firstSentAt, 100);
    assert.equal(byId.get("R2").firstSentAt, 250);
    for (const request of raw.requests) {
      assert.ok(request.firstSentAt >= request.plannedArrival);
    }
  });

  test("即使会话空闲，迟到的到达也不会被提前捎带", () => {
    const records = [
      baseRecord({ logicalId: "R1", plannedArrival: 0 }),
      baseRecord({ logicalId: "R2", plannedArrival: 500 }),
    ];
    const raw = runSinglePath(records, successPath(10), "p");
    const r2 = raw.requests.find((r) => r.logicalId === "R2");
    assert.equal(r2.firstSentAt, 500);
    assert.equal(r2.waitMs, 0);
  });
});

describe("会话依赖：前序成功后才能发出后继", () => {
  test("链上请求按完成顺序串行，等待来自前序耗时", () => {
    const records = [
      baseRecord({ logicalId: "C1", plannedArrival: 0 }),
      baseRecord({ logicalId: "C2", plannedArrival: 0, predecessor: "C1" }),
      baseRecord({ logicalId: "C3", plannedArrival: 0, predecessor: "C2" }),
    ];
    const raw = runSinglePath(records, successPath(20), "p");
    const byId = new Map(raw.requests.map((r) => [r.logicalId, r]));
    assert.equal(byId.get("C1").firstSentAt, 0);
    assert.equal(byId.get("C2").firstSentAt, 20);
    assert.equal(byId.get("C3").firstSentAt, 40);
    assert.equal(byId.get("C3").waitMs, 40);
    assert.deepEqual(
      raw.requests.map((r) => r.finalStatus),
      [FINAL_STATUS.SUCCEEDED, FINAL_STATUS.SUCCEEDED, FINAL_STATUS.SUCCEEDED],
    );
  });

  test("前序最终失败时，后继从未发出并标记 dependency", () => {
    const records = [
      baseRecord({
        logicalId: "C1",
        retryPolicy: { maxAttempts: 1 },
      }),
      baseRecord({ logicalId: "C2", predecessor: "C1" }),
    ];
    const raw = runSinglePath(
      records,
      {
        sessionConcurrency: 1,
        outcome: (id) =>
          id === "C1"
            ? { status: "failure", latencyMs: 5, retryable: false }
            : { status: "success", latencyMs: 5 },
      },
      "p",
    );
    const byId = new Map(raw.requests.map((r) => [r.logicalId, r]));
    assert.equal(byId.get("C1").finalStatus, FINAL_STATUS.FAILED);
    assert.equal(byId.get("C2").finalStatus, FINAL_STATUS.NEVER_SENT);
    assert.equal(byId.get("C2").blockingReason, "dependency");
    assert.equal(byId.get("C2").attemptCount, 0);
  });
});

describe("同会话并发上限", () => {
  test("无依赖但 cap=1 时按到达顺序排队，不并发", () => {
    const records = [
      baseRecord({ logicalId: "Q1", plannedArrival: 0 }),
      baseRecord({ logicalId: "Q2", plannedArrival: 0 }),
      baseRecord({ logicalId: "Q3", plannedArrival: 0 }),
    ];
    const raw = runSinglePath(records, successPath(30), "p");
    const byId = new Map(raw.requests.map((r) => [r.logicalId, r]));
    assert.equal(byId.get("Q1").firstSentAt, 0);
    assert.equal(byId.get("Q2").firstSentAt, 30);
    assert.equal(byId.get("Q3").firstSentAt, 60);
  });

  test("cap=2 时同会话最多两个在途，第三个等槽位", () => {
    const records = [
      baseRecord({ logicalId: "Q1", plannedArrival: 0 }),
      baseRecord({ logicalId: "Q2", plannedArrival: 0 }),
      baseRecord({ logicalId: "Q3", plannedArrival: 0 }),
    ];
    const raw = runSinglePath(
      records,
      { sessionConcurrency: 2, outcome: () => ({ status: "success", latencyMs: 40 }) },
      "p",
    );
    const byId = new Map(raw.requests.map((r) => [r.logicalId, r]));
    assert.equal(byId.get("Q1").firstSentAt, 0);
    assert.equal(byId.get("Q2").firstSentAt, 0);
    assert.equal(byId.get("Q3").firstSentAt, 40);
    assert.equal(byId.get("Q3").waitMs, 40);
  });

  test("不同会话互不挤占：A 的排队不影响 B 的准时发出", () => {
    const records = [
      baseRecord({ logicalId: "A1", plannedArrival: 0, sessionId: "A" }),
      baseRecord({ logicalId: "A2", plannedArrival: 0, sessionId: "A" }),
      baseRecord({ logicalId: "B1", plannedArrival: 0, sessionId: "B" }),
    ];
    const raw = runSinglePath(
      records,
      { sessionConcurrency: 1, outcome: () => ({ status: "success", latencyMs: 40 }) },
      "p",
    );
    const byId = new Map(raw.requests.map((r) => [r.logicalId, r]));
    assert.equal(byId.get("A1").firstSentAt, 0);
    assert.equal(byId.get("A2").firstSentAt, 40);
    assert.equal(byId.get("B1").firstSentAt, 0);
  });
});

describe("重试独立发生且归属原逻辑请求", () => {
  test("可重试失败按 backoff 再发，成功后后继才发出", () => {
    const records = [
      baseRecord({
        logicalId: "R1",
        plannedArrival: 0,
        retryPolicy: { maxAttempts: 3, backoffMs: 50 },
      }),
      baseRecord({ logicalId: "R2", plannedArrival: 0, predecessor: "R1" }),
    ];
    const raw = runSinglePath(
      records,
      {
        sessionConcurrency: 1,
        outcome: (id, attempt) => {
          if (id === "R1" && attempt < 3) {
            return { status: "failure", latencyMs: 10, retryable: true };
          }
          return { status: "success", latencyMs: 10 };
        },
      },
      "p",
    );
    const r1 = raw.requests.find((r) => r.logicalId === "R1");
    assert.equal(r1.attemptCount, 3);
    assert.deepEqual(
      r1.attempts.map((a) => a.sentAt),
      [0, 60, 120],
    );
    assert.deepEqual(
      r1.attempts.map((a) => a.completedAt),
      [10, 70, 130],
    );
    assert.equal(r1.finalStatus, FINAL_STATUS.SUCCEEDED);
    const r2 = raw.requests.find((r) => r.logicalId === "R2");
    assert.equal(r2.firstSentAt, 130);
  });

  test("达到 maxAttempts 后失败，不再重试", () => {
    const records = [
      baseRecord({
        logicalId: "R1",
        retryPolicy: { maxAttempts: 2, backoffMs: 10 },
      }),
    ];
    const raw = runSinglePath(
      records,
      {
        sessionConcurrency: 1,
        outcome: () => ({ status: "failure", latencyMs: 5, retryable: true }),
      },
      "p",
    );
    const r1 = raw.requests[0];
    assert.equal(r1.attemptCount, 2);
    assert.equal(r1.finalStatus, FINAL_STATUS.FAILED);
    assert.deepEqual(
      r1.attempts.map((a) => a.sentAt),
      [0, 15],
    );
  });

  test("不可重试失败立即终止，不消耗后续尝试名额", () => {
    const records = [
      baseRecord({
        logicalId: "R1",
        retryPolicy: { maxAttempts: 5, backoffMs: 10 },
      }),
    ];
    const raw = runSinglePath(
      records,
      {
        sessionConcurrency: 1,
        outcome: () => ({ status: "failure", latencyMs: 5, retryable: false }),
      },
      "p",
    );
    assert.equal(raw.requests[0].attemptCount, 1);
    assert.equal(raw.requests[0].finalStatus, FINAL_STATUS.FAILED);
  });
});

describe("挂起与截止时刻", () => {
  test("挂起请求占用槽位直到截止，后继从未发出", () => {
    const records = [
      baseRecord({ logicalId: "H1", plannedArrival: 0 }),
      baseRecord({ logicalId: "H2", plannedArrival: 0, predecessor: "H1" }),
      baseRecord({ logicalId: "H3", plannedArrival: 0 }),
    ];
    const raw = runSinglePath(
      records,
      {
        sessionConcurrency: 1,
        deadlineMs: 500,
        outcome: (id) =>
          id === "H1" ? { status: "hang" } : { status: "success", latencyMs: 10 },
      },
      "p",
    );
    const byId = new Map(raw.requests.map((r) => [r.logicalId, r]));
    assert.equal(byId.get("H1").finalStatus, FINAL_STATUS.IN_FLIGHT);
    assert.equal(byId.get("H1").attemptCount, 1);
    assert.equal(byId.get("H2").finalStatus, FINAL_STATUS.NEVER_SENT);
    assert.equal(byId.get("H2").blockingReason, "dependency");
    // H3 与 H1 同会话，槽位被挂起请求占住。
    assert.equal(byId.get("H3").finalStatus, FINAL_STATUS.NEVER_SENT);
    assert.equal(byId.get("H3").blockingReason, "concurrency-cap");
  });

  test("截止时刻边界：t=deadline 的事件仍处理，之后的到达不再发出", () => {
    const records = [
      baseRecord({ logicalId: "E1", plannedArrival: 100 }),
      baseRecord({ logicalId: "E2", plannedArrival: 101, sessionId: "s2" }),
    ];
    const raw = runSinglePath(
      records,
      {
        sessionConcurrency: 1,
        deadlineMs: 100,
        outcome: () => ({ status: "success", latencyMs: 0 }),
      },
      "p",
    );
    const byId = new Map(raw.requests.map((r) => [r.logicalId, r]));
    assert.equal(byId.get("E1").finalStatus, FINAL_STATUS.SUCCEEDED);
    assert.equal(byId.get("E1").firstSentAt, 100);
    assert.equal(byId.get("E2").finalStatus, FINAL_STATUS.NEVER_SENT);
    assert.equal(byId.get("E2").blockingReason, "deadline-before-arrival");
  });

  test("截止时仍在等待 backoff 的重试标记 retry-not-reached", () => {
    const records = [
      baseRecord({
        logicalId: "R1",
        retryPolicy: { maxAttempts: 3, backoffMs: 100 },
      }),
    ];
    const raw = runSinglePath(
      records,
      {
        sessionConcurrency: 1,
        deadlineMs: 50,
        outcome: () => ({ status: "failure", latencyMs: 10, retryable: true }),
      },
      "p",
    );
    const r1 = raw.requests[0];
    assert.equal(r1.attemptCount, 1);
    assert.equal(r1.finalStatus, FINAL_STATUS.NEVER_SENT);
    assert.equal(r1.blockingReason, "retry-not-reached");
  });

  test("不给 deadline 时模拟跑到自然结束", () => {
    const records = [baseRecord({ logicalId: "R1", plannedArrival: 30 })];
    const raw = runSinglePath(
      records,
      { sessionConcurrency: 1, outcome: () => ({ status: "success", latencyMs: 25 }) },
      "p",
    );
    assert.equal(raw.deadlineMs, null);
    assert.equal(raw.endedAt, 55);
    assert.equal(raw.requests[0].finalStatus, FINAL_STATUS.SUCCEEDED);
  });
});

describe("输入校验与确定性", () => {
  test("重复 logicalId 被拒绝", () => {
    const records = [
      baseRecord({ logicalId: "X" }),
      baseRecord({ logicalId: "X", sessionId: "s2" }),
    ];
    assert.throws(() => runSinglePath(records, successPath(), "p"), /logicalId 重复/);
  });

  test("跨会话前序引用被拒绝", () => {
    const records = [
      baseRecord({ logicalId: "X", sessionId: "a" }),
      baseRecord({ logicalId: "Y", sessionId: "b", predecessor: "X" }),
    ];
    assert.throws(() => runSinglePath(records, successPath(), "p"), /同一会话/);
  });

  test("缺失的前序引用被拒绝", () => {
    const records = [baseRecord({ logicalId: "Y", predecessor: "ghost" })];
    assert.throws(() => runSinglePath(records, successPath(), "p"), /前序/);
  });

  test("非法时间与策略字段被拒绝", () => {
    assert.throws(
      () =>
        runSinglePath(
          [baseRecord({ logicalId: "Y", plannedArrival: -1 })],
          successPath(),
          "p",
        ),
      /plannedArrival/,
    );
    assert.throws(
      () =>
        runSinglePath(
          [baseRecord({ logicalId: "Y", retryPolicy: { maxAttempts: 0 } })],
          successPath(),
          "p",
        ),
      /maxAttempts/,
    );
  });

  test("同样输入两次运行结果完全一致（确定性）", () => {
    const records = [
      baseRecord({
        logicalId: "R1",
        retryPolicy: { maxAttempts: 3, backoffMs: 10 },
      }),
      baseRecord({ logicalId: "R2", predecessor: "R1", plannedArrival: 5 }),
    ];
    const config = {
      sessionConcurrency: 1,
      outcome: (id, attempt) =>
        attempt === 1
          ? { status: "failure", latencyMs: 8, retryable: true }
          : { status: "success", latencyMs: 12 },
    };
    const run1 = runSinglePath(records, config, "p");
    const run2 = runSinglePath(records, config, "p");
    assert.deepEqual(run1, run2);
  });

  test("空记录集返回空结果且不报错", () => {
    const raw = runSinglePath([], successPath(), "p");
    assert.deepEqual(raw.requests, []);
    assert.equal(raw.endedAt, 0);
  });
});

describe("MinHeap 同键 FIFO", () => {
  test("相同键按入队顺序出队", () => {
    const heap = new MinHeap();
    heap.push(5, "a");
    heap.push(5, "b");
    heap.push(3, "c");
    heap.push(5, "d");
    assert.deepEqual(
      [heap.pop(), heap.pop(), heap.pop(), heap.pop()],
      ["c", "a", "b", "d"],
    );
  });
});
