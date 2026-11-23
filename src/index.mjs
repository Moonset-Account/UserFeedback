// 双路回放模块对外入口。
//
// 用法：
//   const result = runDualPathReplay(records, { baseline: baselineConfig, candidate: candidateConfig });
//   result.report      每路计划/发出/从未发出/完成情况 + 按逻辑 ID 的双路配对
//   result.explanation 挂起/重试定位、会话等待对比与文字解释

import { normalizeRecords, normalizeConfig } from "./records.mjs";
import { simulatePath, REQUEST_STATUS, FINAL_STATUS } from "./engine.mjs";
import { buildReport } from "./report.mjs";
import { explain } from "./explanation.mjs";

export { REQUEST_STATUS, FINAL_STATUS };

/**
 * 执行双路回放。
 *
 * @param {Array} records  合成请求记录（共享的同一份初次到达安排）
 * @param {object} configs { baseline: 路配置, candidate: 路配置 }
 * @returns {{ report: object, explanation: object, raw: object }}
 */
export function runDualPathReplay(records, configs) {
  if (configs === null || typeof configs !== "object") {
    throw new TypeError("configs 必须包含 baseline 与 candidate 两路配置");
  }
  const normalizedRecords = normalizeRecords(records);
  const baselineConfig = normalizeConfig({ label: "baseline", ...configs.baseline });
  const candidateConfig = normalizeConfig({ label: "candidate", ...configs.candidate });

  // 两路拿到的是同一份（规范化后的）不可变到达安排；各自独立模拟。
  const rawBaseline = simulatePath(normalizedRecords, baselineConfig);
  const rawCandidate = simulatePath(normalizedRecords, candidateConfig);

  const report = buildReport(rawBaseline, rawCandidate, normalizedRecords);
  const explanation = explain(report);

  return { report, explanation, raw: { baseline: rawBaseline, candidate: rawCandidate } };
}

/** 只跑单路（便于单测与单独观察一路行为）。 */
export function runSinglePath(records, pathConfig, label = "single") {
  const normalizedRecords = normalizeRecords(records);
  const config = normalizeConfig({ label, ...pathConfig });
  return simulatePath(normalizedRecords, config);
}
