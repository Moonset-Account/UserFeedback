import { GateInputError, ReasonCode, Source } from './errors.js';
import { makeFinding } from './semantics.js';

/**
 * 回执时效门禁（迟到的旧解析回执不能替代当前执行版本的结果）。
 *
 * 台账 ledger 结构：
 *   { [service]: { digest, versionLabel, versionOrdinal, sequence, receivedAt } }
 * 纯函数设计：不就地修改调用方台账，返回新台账；失效回执不会推进台账。
 */

function validateReceipt(receipt) {
  if (!receipt || typeof receipt !== 'object') throw new GateInputError('回执必须为对象');
  const { service, digest, versionLabel, sequence } = receipt;
  if (typeof service !== 'string' || !service) throw new GateInputError('回执缺少 service');
  if (typeof digest !== 'string' || !digest) throw new GateInputError(`服务 ${service} 的回执缺少 digest`);
  if (typeof versionLabel !== 'string' || !versionLabel) throw new GateInputError(`服务 ${service} 的回执缺少 versionLabel`);
  if (!Number.isInteger(sequence) || sequence < 1) throw new GateInputError(`服务 ${service} 的回执 sequence 必须为 >=1 的整数`);
}

/**
 * 校验单条回执。
 * @param {object} receipt  当前收到的解析回执
 * @param {object} expectedDigest 本次请求摘要（必须与回执绑定同一摘要）
 * @param {object} currentTable 该服务当前生效版本的语义表
 * @param {Array}  allTables  该服务全部已知版本语义表（按 ordinal 升序）
 * @param {object|undefined} ledgerEntry 台账中该服务的上一条已接受回执
 * @returns {{finding?: object, accepted: boolean}}
 */
export function inspectReceipt(receipt, expectedDigest, currentTable, allTables, ledgerEntry) {
  validateReceipt(receipt);
  const loc = `receipt:${receipt.service}`;

  if (receipt.digest !== expectedDigest) {
    return {
      accepted: false,
      finding: makeFinding(
        ReasonCode.RECEIPT_DIGEST_MISMATCH,
        Source.GATE,
        loc,
        `回执绑定摘要 ${receipt.digest} 与请求摘要 ${expectedDigest} 不一致，拒绝串单回执`,
        { expectedDigest, receiptDigest: receipt.digest }
      )
    };
  }

  const table = allTables.find((t) => t.versionLabel === receipt.versionLabel);
  if (!table) {
    return {
      accepted: false,
      finding: makeFinding(
        ReasonCode.RECEIPT_UNKNOWN_VERSION,
        Source.GATE,
        loc,
        `回执版本 ${receipt.versionLabel} 在 ${receipt.service} 的语义表中不存在`,
        { receiptVersion: receipt.versionLabel }
      )
    };
  }

  // 版本落后于当前执行版本：旧解析结果迟到，不能替代当前版本
  if (table.versionOrdinal < currentTable.versionOrdinal) {
    return {
      accepted: false,
      finding: makeFinding(
        ReasonCode.RECEIPT_STALE,
        Source.GATE,
        loc,
        `回执来自旧版本 ${receipt.versionLabel}(ordinal=${table.versionOrdinal})，当前执行版本为 ${currentTable.versionLabel}(ordinal=${currentTable.versionOrdinal})，旧解析回执不替代当前结果`,
        { receiptVersion: receipt.versionLabel, currentVersion: currentTable.versionLabel }
      )
    };
  }
  // 未知的未来版本同样失败关闭
  if (table.versionOrdinal > currentTable.versionOrdinal) {
    return {
      accepted: false,
      finding: makeFinding(
        ReasonCode.RECEIPT_FUTURE_VERSION,
        Source.GATE,
        loc,
        `回执来自未知的未来版本 ${receipt.versionLabel}，当前执行版本为 ${currentTable.versionLabel}`,
        { receiptVersion: receipt.versionLabel, currentVersion: currentTable.versionLabel }
      )
    };
  }

  // 同版本内按 sequence 防重放/防倒退
  if (ledgerEntry && ledgerEntry.digest === expectedDigest && receipt.sequence <= ledgerEntry.sequence) {
    return {
      accepted: false,
      finding: makeFinding(
        ReasonCode.RECEIPT_DUPLICATE,
        Source.GATE,
        loc,
        `回执 sequence=${receipt.sequence} 未超过已登记 sequence=${ledgerEntry.sequence}，疑似重复或迟到回执`,
        { receiptSequence: receipt.sequence, ledgerSequence: ledgerEntry.sequence }
      )
    };
  }

  return { accepted: true };
}

/**
 * 处理一次门禁调用中的全部回执，并产出新台账。
 * 每个服务只允许一条有效回执；同一服务出现多条 -> RECEIPT_DUPLICATE。
 * 缺失当前版本回执 -> RECEIPT_MISSING（由门禁在调用后判定）。
 */
export function processReceipts(input) {
  const { digest, receipts, semanticTablesByService, ledger = {} } = input;
  if (typeof digest !== 'string' || !digest) throw new GateInputError('门禁输入缺少请求摘要 digest');
  if (!Array.isArray(receipts)) throw new GateInputError('receipts 必须为数组');

  const findings = [];
  const nextLedger = structuredClone(ledger);
  const seenServices = new Set();

  for (const receipt of receipts) {
    validateReceipt(receipt);
    if (seenServices.has(receipt.service)) {
      findings.push(
        makeFinding(ReasonCode.RECEIPT_DUPLICATE, Source.GATE, `receipt:${receipt.service}`, `服务 ${receipt.service} 在同一次调用中出现多条回执`)
      );
      continue;
    }
    seenServices.add(receipt.service);

    const tables = semanticTablesByService[receipt.service];
    if (!tables || !tables.length) {
      throw new GateInputError(`缺少服务 ${receipt.service} 的语义表，无法校验回执`);
    }
    const sorted = [...tables].sort((a, b) => a.versionOrdinal - b.versionOrdinal);
    const currentTable = sorted.at(-1);
    const result = inspectReceipt(receipt, digest, currentTable, sorted, nextLedger[receipt.service]);
    if (!result.accepted) {
      findings.push(result.finding);
      continue;
    }
    // 仅接受的回执推进台账，失效回执不覆盖既有状态
    nextLedger[receipt.service] = {
      digest,
      versionLabel: receipt.versionLabel,
      versionOrdinal: currentTable.versionOrdinal,
      sequence: receipt.sequence,
      receivedAt: receipt.receivedAt ?? new Date().toISOString()
    };
  }

  return { findings, ledger: nextLedger, seenServices };
}
