// 合成请求记录的校验与规范化。
// 一份记录只描述一个逻辑请求（logicalId）；重试不是新记录，由模拟器按 retryPolicy 产生。

const REQUIRED_FIELDS = ["logicalId", "plannedArrival", "sessionId", "retryPolicy"];

function assertNonNegativeInteger(value, label) {
  if (!Number.isInteger(value) || value < 0) {
    throw new TypeError(`${label} 必须是非负整数，收到: ${String(value)}`);
  }
}

function assertPositiveInteger(value, label) {
  if (!Number.isInteger(value) || value <= 0) {
    throw new TypeError(`${label} 必须是正整数，收到: ${String(value)}`);
  }
}

function normalizeRetryPolicy(raw, logicalId) {
  if (raw === null || typeof raw !== "object") {
    throw new TypeError(`请求 ${logicalId} 的 retryPolicy 必须是对象`);
  }
  assertNonNegativeInteger(raw.maxAttempts, `请求 ${logicalId} 的 retryPolicy.maxAttempts`);
  if (raw.maxAttempts < 1) {
    throw new TypeError(`请求 ${logicalId} 的 retryPolicy.maxAttempts 至少为 1`);
  }
  assertNonNegativeInteger(raw.backoffMs ?? 0, `请求 ${logicalId} 的 retryPolicy.backoffMs`);
  return {
    maxAttempts: raw.maxAttempts,
    backoffMs: raw.backoffMs ?? 0,
  };
}

/**
 * 校验并规范化一组合成请求记录（不修改输入，返回深拷贝）。
 *
 * 记录字段：
 * - logicalId        逻辑请求 ID，全局唯一
 * - round            到达轮次（可选，默认 1），用于区分多轮初始负载
 * - plannedArrival   计划初次到达时刻（非负整数毫秒）
 * - sessionId        所属会话 ID
 * - predecessor      同一会话内的前序逻辑请求 ID（可选；无则为 null）
 * - retryPolicy      { maxAttempts, backoffMs }
 */
export function normalizeRecords(input) {
  if (!Array.isArray(input)) {
    throw new TypeError("输入记录必须是数组");
  }
  const seenIds = new Set();
  const records = input.map((raw, index) => {
    if (raw === null || typeof raw !== "object") {
      throw new TypeError(`第 ${index} 条记录不是对象`);
    }
    for (const field of REQUIRED_FIELDS) {
      if (raw[field] === undefined || raw[field] === null) {
        throw new TypeError(`第 ${index} 条记录缺少必填字段 ${field}`);
      }
    }
    const logicalId = String(raw.logicalId);
    if (seenIds.has(logicalId)) {
      throw new TypeError(`logicalId 重复: ${logicalId}`);
    }
    seenIds.add(logicalId);

    assertNonNegativeInteger(raw.plannedArrival, `请求 ${logicalId} 的 plannedArrival`);
    const sessionId = String(raw.sessionId);
    const round = raw.round === undefined || raw.round === null ? 1 : raw.round;
    assertPositiveInteger(round, `请求 ${logicalId} 的 round`);
    const predecessor =
      raw.predecessor === undefined || raw.predecessor === null ? null : String(raw.predecessor);

    return {
      logicalId,
      round,
      plannedArrival: raw.plannedArrival,
      sessionId,
      predecessor,
      retryPolicy: normalizeRetryPolicy(raw.retryPolicy, logicalId),
    };
  });

  // 前序引用必须存在且属于同一会话。
  const byId = new Map(records.map((r) => [r.logicalId, r]));
  for (const record of records) {
    if (record.predecessor !== null) {
      const predecessor = byId.get(record.predecessor);
      if (!predecessor) {
        throw new TypeError(
          `请求 ${record.logicalId} 的前序 ${record.predecessor} 不存在`,
        );
      }
      if (predecessor.sessionId !== record.sessionId) {
        throw new TypeError(
          `请求 ${record.logicalId} 与前序 ${record.predecessor} 不属于同一会话`,
        );
      }
    }
  }
  return records;
}

/**
 * 规范化单路（基线或候选）的行为配置。
 *
 * - label                       路名
 * - sessionConcurrency          每个会话的同会话并发上限（正整数）
 * - outcome(logicalId, attempt) 每次尝试的结果：
 *     { status: "success", latencyMs }
 *     { status: "failure", latencyMs, retryable? }   retryable 默认 true
 *     { status: "hang", latencyMs? }                 永不返回；latencyMs 仅用于报告参考
 * - deadlineMs                  模拟截止时刻（可选；不给则跑到自然结束）
 */
export function normalizeConfig(raw) {
  if (raw === null || typeof raw !== "object") {
    throw new TypeError("路配置必须是对象");
  }
  const label = String(raw.label ?? "");
  if (!label) {
    throw new TypeError("路配置缺少 label");
  }
  assertPositiveInteger(raw.sessionConcurrency, `${label} 的 sessionConcurrency`);
  if (typeof raw.outcome !== "function") {
    throw new TypeError(`${label} 的 outcome 必须是函数`);
  }
  const deadlineMs =
    raw.deadlineMs === undefined || raw.deadlineMs === null ? null : raw.deadlineMs;
  if (deadlineMs !== null) {
    assertNonNegativeInteger(deadlineMs, `${label} 的 deadlineMs`);
  }
  return {
    label,
    sessionConcurrency: raw.sessionConcurrency,
    outcome: raw.outcome,
    deadlineMs,
  };
}
