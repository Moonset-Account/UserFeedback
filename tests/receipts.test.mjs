import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { inspectReceipt, processReceipts, ReasonCode } from '../src/index.js';
import { APPROVAL_SERVICE, EXECUTION_SERVICE, DIGEST, semanticTables } from './fixtures/baseline.js';

const execTables = semanticTables[EXECUTION_SERVICE].sort((a, b) => a.versionOrdinal - b.versionOrdinal);
const currentExec = execTables.at(-1);
const baseReceipt = { service: EXECUTION_SERVICE, digest: DIGEST, versionLabel: 'v3', sequence: 2 };

describe('回执时效 · 单条回执判定', () => {
  it('当前版本且 sequence 更新 -> 接受', () => {
    const ledger = { [EXECUTION_SERVICE]: { digest: DIGEST, versionLabel: 'v3', versionOrdinal: 3, sequence: 1 } };
    const result = inspectReceipt(baseReceipt, DIGEST, currentExec, execTables, ledger[EXECUTION_SERVICE]);
    assert.equal(result.accepted, true);
  });

  it('旧版本回执迟到 -> RECEIPT_STALE，不能替代当前版本', () => {
    const result = inspectReceipt({ ...baseReceipt, versionLabel: 'v2' }, DIGEST, currentExec, execTables, undefined);
    assert.equal(result.accepted, false);
    assert.equal(result.finding.code, ReasonCode.RECEIPT_STALE);
  });

  it('摘要不一致 -> RECEIPT_DIGEST_MISMATCH', () => {
    const result = inspectReceipt({ ...baseReceipt, digest: 'sha256:other' }, DIGEST, currentExec, execTables, undefined);
    assert.equal(result.finding.code, ReasonCode.RECEIPT_DIGEST_MISMATCH);
  });

  it('未知版本标签 -> RECEIPT_UNKNOWN_VERSION；未来 ordinal -> RECEIPT_FUTURE_VERSION', () => {
    const unknown = inspectReceipt({ ...baseReceipt, versionLabel: 'v9' }, DIGEST, currentExec, execTables, undefined);
    assert.equal(unknown.finding.code, ReasonCode.RECEIPT_UNKNOWN_VERSION);

    const futureTables = [
      ...execTables,
      { service: EXECUTION_SERVICE, versionLabel: 'v4', versionOrdinal: 4, fields: currentExec.fields }
    ];
    const future = inspectReceipt({ ...baseReceipt, versionLabel: 'v4' }, DIGEST, currentExec, futureTables, undefined);
    assert.equal(future.finding.code, ReasonCode.RECEIPT_FUTURE_VERSION);
  });

  it('同版本 sequence 未增长 -> RECEIPT_DUPLICATE', () => {
    const ledger = { digest: DIGEST, versionLabel: 'v3', versionOrdinal: 3, sequence: 2 };
    const result = inspectReceipt(baseReceipt, DIGEST, currentExec, execTables, ledger);
    assert.equal(result.accepted, false);
    assert.equal(result.finding.code, ReasonCode.RECEIPT_DUPLICATE);
  });

  it('上一摘要的台账存在时，同 sequence 的新摘要回执仍视为首条，允许登记', () => {
    const ledger = { digest: 'sha256:old-request', versionLabel: 'v3', versionOrdinal: 3, sequence: 9 };
    const result = inspectReceipt({ ...baseReceipt, sequence: 1 }, DIGEST, currentExec, execTables, ledger);
    assert.equal(result.accepted, true);
  });
});

describe('回执时效 · 台账处理', () => {
  const tables = semanticTables;

  it('有效回执推进台账，失效回执不推进且返回独立新对象', () => {
    const ledger = {};
    const result = processReceipts({
      digest: DIGEST,
      receipts: [
        { service: APPROVAL_SERVICE, digest: DIGEST, versionLabel: 'v2', sequence: 1 },
        { service: EXECUTION_SERVICE, digest: DIGEST, versionLabel: 'v2', sequence: 1 } // 旧版本
      ],
      semanticTablesByService: tables,
      ledger
    });
    assert.ok(result.findings.some((f) => f.code === ReasonCode.RECEIPT_STALE));
    assert.equal(result.ledger[EXECUTION_SERVICE], undefined);
    assert.equal(result.ledger[APPROVAL_SERVICE].sequence, 1);
    assert.notEqual(result.ledger, ledger);
    assert.deepEqual(ledger, {});
  });

  it('同一服务同批多条回执 -> RECEIPT_DUPLICATE，只处理首条', () => {
    const result = processReceipts({
      digest: DIGEST,
      receipts: [
        { service: EXECUTION_SERVICE, digest: DIGEST, versionLabel: 'v3', sequence: 1 },
        { service: EXECUTION_SERVICE, digest: DIGEST, versionLabel: 'v3', sequence: 2 }
      ],
      semanticTablesByService: tables,
      ledger: {}
    });
    assert.ok(result.findings.some((f) => f.code === ReasonCode.RECEIPT_DUPLICATE));
    assert.equal(result.ledger[EXECUTION_SERVICE].sequence, 1);
  });

  it('跨请求交错：旧请求的迟到回执因摘要不符被拒，不污染当前请求台账', () => {
    const ledger = {
      [EXECUTION_SERVICE]: { digest: DIGEST, versionLabel: 'v3', versionOrdinal: 3, sequence: 5 }
    };
    const result = processReceipts({
      digest: DIGEST,
      receipts: [{ service: EXECUTION_SERVICE, digest: 'sha256:old-request', versionLabel: 'v3', sequence: 6 }],
      semanticTablesByService: tables,
      ledger
    });
    assert.equal(result.findings[0].code, ReasonCode.RECEIPT_DIGEST_MISMATCH);
    assert.equal(result.ledger[EXECUTION_SERVICE].sequence, 5);
    assert.equal(result.ledger[EXECUTION_SERVICE].digest, DIGEST);
  });
});
