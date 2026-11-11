import { inspectPair } from './inspector.js';
import { canonicalHash } from './digest.js';

/**
 * 导入存储：负责"同一批结果重复导入"场景下的幂等。
 *
 * 两层幂等保证：
 *  1. inspectPair 是纯函数，输入相同 => reportId / issueId 字节级一致；
 *  2. issueId 是内容寻址哈希，store 按 issueId 去重，跨批次出现的同一问题只记一次；
 *  3. 同一 batchId 重复导入完全相同的输入，直接返回去重结果，不产生任何新问题。
 *     同一 batchId 但输入内容变化时抛错，避免静默覆盖。
 */
export class ReportStore {
  constructor() {
    this.batchInputHashes = new Map(); // batchId -> 输入内容哈希
    this.batchIssueIds = new Map();    // batchId -> [issueId]
    this.issuesById = new Map();       // issueId -> issue（全局去重）
    this.reportIdsByBatch = new Map(); // batchId -> reportId
  }

  importPair(rawGateway, rawBusiness, batchId) {
    const inputHash = canonicalHash({ gateway: rawGateway, business: rawBusiness });
    const key = batchId === undefined || batchId === null ? null : String(batchId);

    if (key !== null && this.batchInputHashes.has(key)) {
      const previousHash = this.batchInputHashes.get(key);
      if (previousHash !== inputHash) {
        throw new Error(`批次 ${key} 曾以不同内容导入（${previousHash} ≠ ${inputHash}），拒绝覆盖；请使用新的批次号`);
      }
      return {
        imported: false,
        deduped: true,
        batchId: key,
        inputHash,
        reportId: this.reportIdsByBatch.get(key),
        newIssueCount: 0,
        skippedIssueCount: this.batchIssueIds.get(key).length,
        report: this._snapshotForBatch(key),
      };
    }

    const report = inspectPair(rawGateway, rawBusiness);
    let newIssueCount = 0;
    let skippedIssueCount = 0;
    const issueIds = [];

    for (const issue of report.issues) {
      issueIds.push(issue.issueId);
      if (this.issuesById.has(issue.issueId)) {
        skippedIssueCount += 1;
        continue;
      }
      this.issuesById.set(issue.issueId, issue);
      newIssueCount += 1;
    }

    if (key !== null) {
      this.batchInputHashes.set(key, inputHash);
      this.batchIssueIds.set(key, issueIds);
      this.reportIdsByBatch.set(key, report.reportId);
    }

    return { imported: true, deduped: false, batchId: key, inputHash, reportId: report.reportId, newIssueCount, skippedIssueCount, report };
  }

  _snapshotForBatch(key) {
    const issues = this.batchIssueIds.get(key)
      .map((id) => this.issuesById.get(id))
      .sort((a, b) => a.issueId.localeCompare(b.issueId));
    const countsByType = {};
    for (const issue of issues) countsByType[issue.type] = (countsByType[issue.type] ?? 0) + 1;
    return {
      reportId: this.reportIdsByBatch.get(key),
      generatedAt: null,
      blocking: issues.length > 0,
      summary: { issueCount: issues.length, countsByType },
      issues,
    };
  }

  /** 跨批次合并视图：当前存储中全部去重后的问题。 */
  mergedReport() {
    const issues = [...this.issuesById.values()].sort((a, b) => a.issueId.localeCompare(b.issueId));
    const countsByType = {};
    for (const issue of issues) countsByType[issue.type] = (countsByType[issue.type] ?? 0) + 1;
    return {
      blocking: issues.length > 0,
      summary: {
        issueCount: issues.length,
        countsByType,
        batchCount: this.batchInputHashes.size,
      },
      issues,
    };
  }
}
