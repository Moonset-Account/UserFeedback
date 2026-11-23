// 单路回放引擎（离散事件模拟，虚拟时钟，不依赖真实时间）。
//
// 每一路独立运行：
//   1. 初次到达时刻来自共享的同一份记录，路内不得提前发出；
//   2. 发出前必须同时满足：已到达、同会话前序已成功、同会话在途数低于并发上限；
//   3. 失败后的重试在本路内按自身 backoff 独立发生，仍归属原 logicalId，不产生新记录。

import { MinHeap } from "./priority-queue.mjs";

export const REQUEST_STATUS = Object.freeze({
  PENDING: "pending",
  IN_FLIGHT: "in-flight",
  AWAITING_RETRY: "awaiting-retry",
  SUCCEEDED: "succeeded",
  FAILED: "failed",
});

// 终态报告分类（含截止时刻下的未完成态）。
export const FINAL_STATUS = Object.freeze({
  SUCCEEDED: "succeeded",
  FAILED: "failed",
  IN_FLIGHT: "in-flight",
  NEVER_SENT: "never-sent",
});

function resolveOutcome(outcomeFn, logicalId, attempt) {
  let raw;
  try {
    raw = outcomeFn(logicalId, attempt);
  } catch (error) {
    throw new Error(
      `路的 outcome 回调在请求 ${logicalId} 第 ${attempt} 次尝试时抛出异常: ${error.message}`,
      { cause: error },
    );
  }
  if (raw === null || typeof raw !== "object") {
    throw new TypeError(`请求 ${logicalId} 第 ${attempt} 次尝试的 outcome 不是对象`);
  }
  if (raw.status === "hang") {
    const latencyMs =
      Number.isInteger(raw.latencyMs) && raw.latencyMs >= 0 ? raw.latencyMs : null;
    return { status: "hang", latencyMs };
  }
  if (raw.status !== "success" && raw.status !== "failure") {
    throw new TypeError(
      `请求 ${logicalId} 第 ${attempt} 次尝试的 outcome.status 非法: ${String(raw.status)}`,
    );
  }
  if (!Number.isInteger(raw.latencyMs) || raw.latencyMs < 0) {
    throw new TypeError(
      `请求 ${logicalId} 第 ${attempt} 次尝试的 latencyMs 必须是非负整数`,
    );
  }
  if (raw.status === "failure") {
    return {
      status: "failure",
      latencyMs: raw.latencyMs,
      retryable: raw.retryable === undefined ? true : Boolean(raw.retryable),
    };
  }
  return { status: "success", latencyMs: raw.latencyMs };
}

/**
 * 在一条路上执行回放。
 * @param {Array} normalizedRecords normalizeRecords 的输出
 * @param {object}  normalizedConfig  normalizeConfig 的输出
 * @returns 路内原始模拟结果（由 report.mjs 转成对外报告）
 */
export function simulatePath(normalizedRecords, normalizedConfig) {
  const config = normalizedConfig;
  const records = normalizedRecords;

  const arrived = new Set();
  const sessionInFlight = new Map();

  const states = new Map(
    records.map((record) => [
      record.logicalId,
      {
        record,
        status: REQUEST_STATUS.PENDING,
        attempts: [],
        blockingReason: null,
        hung: false,
      },
    ]),
  );

  // 确定性的候选顺序：计划到达时刻、再按逻辑 ID。
  const dispatchOrder = [...records]
    .map((record) => [record.plannedArrival, record.logicalId, record])
    .sort((a, b) => (a[0] === b[0] ? (a[1] < b[1] ? -1 : 1) : a[0] - b[0]))
    .map((entry) => entry[2]);

  const events = new MinHeap();
  for (const record of records) {
    events.push(record.plannedArrival, {
      time: record.plannedArrival,
      type: "arrival",
      logicalId: record.logicalId,
    });
  }

  let now = 0;

  function pump() {
    let progressed = true;
    while (progressed) {
      progressed = false;
      for (const record of dispatchOrder) {
        const state = states.get(record.logicalId);
        // awaiting-retry 只能等 retry-ready 事件唤醒，退避期内不得重发。
        if (state.status !== REQUEST_STATUS.PENDING) {
          continue;
        }
        if (!arrived.has(record.logicalId)) {
          continue;
        }
        if (record.predecessor !== null) {
          const predecessor = states.get(record.predecessor);
          if (predecessor.status !== REQUEST_STATUS.SUCCEEDED) continue;
        }
        const inFlight = sessionInFlight.get(record.sessionId) ?? 0;
        if (inFlight >= config.sessionConcurrency) continue;

        const attemptNumber = state.attempts.length + 1;
        const outcome = resolveOutcome(config.outcome, record.logicalId, attemptNumber);
        state.attempts.push({
          attempt: attemptNumber,
          sentAt: now,
          completedAt: null,
          outcome,
        });
        state.status = REQUEST_STATUS.IN_FLIGHT;
        sessionInFlight.set(record.sessionId, inFlight + 1);
        progressed = true;

        if (outcome.status === "hang") {
          state.hung = true;
          // 挂起：不安排完成事件，占用会话槽位直到截止。
        } else {
          events.push(now + outcome.latencyMs, {
            time: now + outcome.latencyMs,
            type: "complete",
            logicalId: record.logicalId,
            attempt: attemptNumber,
            outcome,
          });
        }
      }
    }
  }

  while (events.size > 0) {
    const nextTime = events.peekKey();
    if (config.deadlineMs !== null && nextTime > config.deadlineMs) break;
    const event = events.pop();
    now = event.time;

    if (event.type === "arrival") {
      arrived.add(event.logicalId);
    } else if (event.type === "retry-ready") {
      // 退避结束：回到可派发状态，由 pump 重新检查依赖与槽位后发出。
      states.get(event.logicalId).status = REQUEST_STATUS.PENDING;
    } else if (event.type === "complete") {
      const state = states.get(event.logicalId);
      const record = state.record;
      const attempt = state.attempts.find((a) => a.attempt === event.attempt);
      attempt.completedAt = now;
      sessionInFlight.set(record.sessionId, (sessionInFlight.get(record.sessionId) ?? 1) - 1);

      if (event.outcome.status === "success") {
        state.status = REQUEST_STATUS.SUCCEEDED;
      } else {
        const canRetry =
          event.outcome.retryable &&
          state.attempts.length < record.retryPolicy.maxAttempts;
        if (canRetry) {
          state.status = REQUEST_STATUS.AWAITING_RETRY;
          events.push(now + record.retryPolicy.backoffMs, {
            time: now + record.retryPolicy.backoffMs,
            type: "retry-ready",
            logicalId: record.logicalId,
          });
        } else {
          state.status = REQUEST_STATUS.FAILED;
        }
      }
    }

    pump();
  }

  // 终态归类与未发出原因。
  const finalized = [];
  for (const record of records) {
    const state = states.get(record.logicalId);
    let finalStatus;
    if (state.status === REQUEST_STATUS.SUCCEEDED) {
      finalStatus = FINAL_STATUS.SUCCEEDED;
    } else if (state.status === REQUEST_STATUS.FAILED) {
      finalStatus = FINAL_STATUS.FAILED;
    } else if (state.status === REQUEST_STATUS.IN_FLIGHT) {
      finalStatus = FINAL_STATUS.IN_FLIGHT;
    } else {
      finalStatus = FINAL_STATUS.NEVER_SENT;
      if (state.status === REQUEST_STATUS.AWAITING_RETRY) {
        state.blockingReason = "retry-not-reached";
      } else if (!arrived.has(record.logicalId)) {
        state.blockingReason = "deadline-before-arrival";
      } else if (
        record.predecessor !== null &&
        states.get(record.predecessor).status !== REQUEST_STATUS.SUCCEEDED
      ) {
        state.blockingReason = "dependency";
      } else {
        state.blockingReason = "concurrency-cap";
      }
    }

    const firstAttempt = state.attempts[0] ?? null;
    const lastAttempt = state.attempts[state.attempts.length - 1] ?? null;
    finalized.push({
      logicalId: record.logicalId,
      round: record.round,
      sessionId: record.sessionId,
      predecessor: record.predecessor,
      plannedArrival: record.plannedArrival,
      finalStatus,
      blockingReason: state.blockingReason,
      attempts: state.attempts.map((a) => ({ ...a, outcome: { ...a.outcome } })),
      attemptCount: state.attempts.length,
      firstSentAt: firstAttempt ? firstAttempt.sentAt : null,
      lastCompletedAt: lastAttempt ? lastAttempt.completedAt : null,
      // 计划到达到初次实际发出之间的等待；从未发出则为 null。
      waitMs: firstAttempt ? firstAttempt.sentAt - record.plannedArrival : null,
    });
  }

  return {
    label: config.label,
    deadlineMs: config.deadlineMs,
    endedAt: now,
    requests: finalized,
  };
}
