import { GateInputError, ReasonCode, Source } from './errors.js';
import { FieldRole, getPath, isScopeNarrower, makeFinding, mapRecordToVersion, restoreOne } from './semantics.js';
import { processReceipts } from './receipts.js';

const DECISION = Object.freeze({ ALLOW: 'ALLOW', BLOCK: 'BLOCK' });

function requireObject(value, name) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new GateInputError(`${name} 必须为对象`);
}

function pickIdentifierField(table) {
  const idField = table.fields.find((f) => f.role === FieldRole.IDENTIFIER);
  if (!idField) throw new GateInputError(`服务 ${table.service} v${table.versionLabel} 的语义表未声明 identifier 字段，无法按业务标识对齐`);
  return idField.path;
}

function currentTableOf(tables, service) {
  if (!Array.isArray(tables) || !tables.length) throw new GateInputError(`缺少服务 ${service} 的语义表`);
  return [...tables].sort((a, b) => a.versionOrdinal - b.versionOrdinal).at(-1);
}

/** 还原一批记录，同时捕获重复业务标识与还原阶段 findings。 */
function restoreBatch(rawList, table, source) {
  if (!Array.isArray(rawList)) throw new GateInputError(`${source} 的解析清单必须为数组`);
  const findings = [];
  const restored = [];
  const idPath = pickIdentifierField(table);
  const seen = new Map();

  rawList.forEach((raw, index) => {
    const result = restoreOne(raw, table, source);
    findings.push(...result.findings);
    const id = getPath(result.record, idPath);
    result.appliedDefaults.forEach((d) => (d.businessId = id ?? `#${index}`));
    if (id !== undefined) {
      if (seen.has(id)) {
        findings.push(
          makeFinding(
            ReasonCode.DUPLICATE_IDENTIFIER,
            source,
            `${idPath}=${id}`,
            `${source} 清单中业务标识 ${id} 出现多次（位置 #${seen.get(id)} 与 #${index}），同一标识冲突，阻止执行`,
            { businessId: id, positions: [seen.get(id), index] }
          )
        );
      } else {
        seen.set(id, index);
      }
    }
    restored.push({ businessId: id, index, ...result });
  });
  return { restored, findings, idPath };
}

/** 审批覆盖参数本身也按审批侧语义表还原（覆盖参数同样存在条件默认值语义）。 */
function restoreCoverage(coverage, table) {
  if (!Array.isArray(coverage)) throw new GateInputError('approval.coverage 必须为数组');
  const idPath = pickIdentifierField(table);
  const map = new Map();
  const findings = [];
  coverage.forEach((entry, index) => {
    const result = restoreOne(entry, table, Source.APPROVAL_COVERAGE);
    findings.push(...result.findings);
    const id = getPath(result.record, idPath);
    if (id === undefined) {
      findings.push(makeFinding(ReasonCode.MISSING_FIELD, Source.APPROVAL_COVERAGE, `${idPath}#${index}`, '审批覆盖参数缺少业务标识'));
      return;
    }
    if (map.has(id)) {
      findings.push(
        makeFinding(ReasonCode.DUPLICATE_IDENTIFIER, Source.APPROVAL_COVERAGE, `${idPath}=${id}`, `审批覆盖参数中业务标识 ${id} 重复`)
      );
    }
    map.set(id, result.record);
  });
  return { map, findings, idPath };
}

function deepEqual(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

/**
 * 逐字段比对一条执行记录（已映射到审批版本词汇表）与审批覆盖记录。
 * 角色化判定：op/target/info 要求一致；scope 允许收窄；security 要求可映射且一致。
 */
function compareRecords(executionRecord, coverageRecord, approvalTable, executionTable, businessId, executionDefaults) {
  const findings = [];
  const loc = (path) => `businessId=${businessId} field=${path}`;

  for (const fieldDef of approvalTable.fields) {
    const path = fieldDef.path;
    const actual = getPath(executionRecord, path);
    const approved = getPath(coverageRecord, path);

    if (actual === undefined) {
      findings.push(makeFinding(ReasonCode.COVERAGE_INCOMPLETE, Source.EXECUTION_PARSE, loc(path), `执行清单缺少审批已覆盖的字段 ${path}`));
      continue;
    }
    if (approved === undefined) {
      // 审批未覆盖而执行侧出现：属于越权新增参数，失败关闭
      findings.push(makeFinding(ReasonCode.PARAM_MISMATCH, Source.EXECUTION_PARSE, loc(path), `字段 ${path} 未被审批覆盖，执行侧不得自行新增`));
      continue;
    }

    switch (fieldDef.role) {
      case FieldRole.IDENTIFIER:
      case FieldRole.OP:
        if (actual !== approved) {
          findings.push(
            makeFinding(ReasonCode.OP_MISMATCH, Source.EXECUTION_PARSE, loc(path), `实际操作与审批不一致：执行=${JSON.stringify(actual)}，审批=${JSON.stringify(approved)}`, {
              actual,
              approved
            })
          );
        }
        break;
      case FieldRole.TARGET:
        if (!deepEqual(actual, approved)) {
          findings.push(
            makeFinding(ReasonCode.TARGET_MISMATCH, Source.EXECUTION_PARSE, loc(path), `操作目标与审批不一致：执行=${JSON.stringify(actual)}，审批=${JSON.stringify(approved)}`, {
              actual,
              approved
            })
          );
        }
        break;
      case FieldRole.SCOPE: {
        if (!isScopeNarrower(approved, actual, fieldDef)) {
          const viaDefault = executionDefaults.some((d) => d.path === path && d.businessId === businessId);
          findings.push(
            makeFinding(
              viaDefault ? ReasonCode.DEFAULT_EXPANDED : ReasonCode.SCOPE_WIDENED,
              Source.EXECUTION_PARSE,
              loc(path),
              viaDefault
                ? `条件默认值把权限范围扩大为 ${JSON.stringify(actual)}，超出审批范围 ${JSON.stringify(approved)}`
                : `权限范围 ${JSON.stringify(actual)} 超出审批范围 ${JSON.stringify(approved)}，仅允许收窄`,
              { actual, approved, viaDefault }
            )
          );
        }
        break;
      }
      case FieldRole.SECURITY:
        if (!deepEqual(actual, approved)) {
          findings.push(
            makeFinding(ReasonCode.SECURITY_MISMATCH, Source.EXECUTION_PARSE, loc(path), `安全选项与审批不一致：执行=${JSON.stringify(actual)}，审批=${JSON.stringify(approved)}`, {
              actual,
              approved
            })
          );
        }
        break;
      case FieldRole.INFO:
        if (!deepEqual(actual, approved)) {
          findings.push(
            makeFinding(ReasonCode.PARAM_MISMATCH, Source.EXECUTION_PARSE, loc(path), `业务参数与审批不一致：执行=${JSON.stringify(actual)}，审批=${JSON.stringify(approved)}`, {
              actual,
              approved
            })
          );
        }
        break;
      default:
        break;
    }
  }
  return findings;
}

/**
 * 执行前门禁主入口。
 *
 * @param {object} input
 * @param {string} input.digest                       绑定的请求摘要
 * @param {object} input.semanticTables               { [service]: 语义表数组（含历史版本） }
 * @param {object} input.approval                     { service, parse:[原始解析记录], coverage:[审批覆盖参数] }
 * @param {object} input.execution                    { service, parse:[原始解析记录] }
 * @param {Array}  input.receipts                     本次收到的解析回执
 * @param {object} [input.ledger]                     历史回执台账（纯函数，不就地修改）
 * @returns {{decision: 'ALLOW'|'BLOCK', findings: Array, ledger: object, effective: object, comparedAt: string}}
 */
export function evaluateGate(input) {
  requireObject(input, 'input');
  const { digest, semanticTables, approval, execution, receipts = [], ledger = {} } = input;
  if (typeof digest !== 'string' || !digest) throw new GateInputError('input.digest 必须为非空字符串（请求摘要）');
  requireObject(semanticTables, 'semanticTables');
  requireObject(approval, 'approval');
  requireObject(execution, 'execution');

  const findings = [];
  const approvalTable = currentTableOf(semanticTables[approval.service], approval.service);
  const executionTable = currentTableOf(semanticTables[execution.service], execution.service);

  // ---- 阶段 1：回执时效（旧版本迟到回执 / 串摘要 / 重放） ----
  const receiptResult = processReceipts({ digest, receipts, semanticTablesByService: semanticTables, ledger });
  findings.push(...receiptResult.findings);
  for (const service of [approval.service, execution.service]) {
    if (!receiptResult.seenServices.has(service)) {
      findings.push(makeFinding(ReasonCode.RECEIPT_MISSING, Source.GATE, `receipt:${service}`, `缺少服务 ${service} 当前版本的解析回执，无法证明解析结果时效`));
    }
  }

  // ---- 阶段 2：按各版本语义表还原有效参数 ----
  const approvalBatch = restoreBatch(approval.parse, approvalTable, Source.APPROVAL_PARSE);
  const executionBatch = restoreBatch(execution.parse, executionTable, Source.EXECUTION_PARSE);
  findings.push(...approvalBatch.findings, ...executionBatch.findings);

  // ---- 阶段 3：审批覆盖参数还原 ----
  const coverageResult = restoreCoverage(approval.coverage, approvalTable);
  findings.push(...coverageResult.findings);

  // ---- 阶段 4：执行侧映射到审批版本词汇表（安全选项无法映射在此拦截） ----
  const mappedExecution = executionBatch.restored.map((entry) => {
    const mapped = mapRecordToVersion(entry.record, executionTable, approvalTable, Source.EXECUTION_PARSE);
    findings.push(...mapped.findings);
    return { ...entry, mappedRecord: mapped.record };
  });

  // ---- 阶段 5：审批侧解析结果必须与审批覆盖参数一致（审批端自身冲突） ----
  for (const entry of approvalBatch.restored) {
    if (entry.businessId === undefined) continue;
    const covered = coverageResult.map.get(entry.businessId);
    if (!covered) {
      findings.push(
        makeFinding(ReasonCode.COVERAGE_INCOMPLETE, Source.APPROVAL_COVERAGE, `businessId=${entry.businessId}`, `审批解析含标识 ${entry.businessId}，但审批覆盖参数未覆盖该项`)
      );
      continue;
    }
    for (const fieldDef of approvalTable.fields) {
      const parsed = getPath(entry.record, fieldDef.path);
      const coveredValue = getPath(covered, fieldDef.path);
      if (!deepEqual(parsed, coveredValue)) {
        findings.push(
          makeFinding(
            ReasonCode.COVERAGE_MISMATCH,
            Source.APPROVAL_PARSE,
            `businessId=${entry.businessId} field=${fieldDef.path}`,
            `审批侧解析结果与审批覆盖参数不一致：解析=${JSON.stringify(parsed)}，覆盖=${JSON.stringify(coveredValue)}`
          )
        );
      }
    }
  }
  for (const coveredId of coverageResult.map.keys()) {
    if (!approvalBatch.restored.some((e) => e.businessId === coveredId)) {
      findings.push(
        makeFinding(ReasonCode.COVERAGE_INCOMPLETE, Source.APPROVAL_PARSE, `businessId=${coveredId}`, `审批覆盖参数含标识 ${coveredId}，但审批解析清单缺失该项`)
      );
    }
  }

  // ---- 阶段 6：按业务标识对齐两份清单 ----
  const approvalIds = new Set(approvalBatch.restored.map((e) => e.businessId).filter((id) => id !== undefined));
  const executionIds = new Set(mappedExecution.map((e) => e.businessId).filter((id) => id !== undefined));
  for (const id of approvalIds) {
    if (!executionIds.has(id)) {
      findings.push(makeFinding(ReasonCode.IDENTIFIER_MISALIGNED, Source.EXECUTION_PARSE, `businessId=${id}`, `审批清单中的标识 ${id} 在执行清单中缺失，清单未按业务标识对齐`));
    }
  }
  for (const id of executionIds) {
    if (!approvalIds.has(id)) {
      findings.push(makeFinding(ReasonCode.IDENTIFIER_CONFLICT, Source.EXECUTION_PARSE, `businessId=${id}`, `执行清单出现审批清单不存在的标识 ${id}，属于越权新增操作`));
    }
  }

  // ---- 阶段 7：逐条与审批覆盖参数比对（操作/目标一致，范围仅可收窄） ----
  const executionDefaults = mappedExecution.flatMap((e) => e.appliedDefaults);
  for (const entry of mappedExecution) {
    if (entry.businessId === undefined) continue;
    const covered = coverageResult.map.get(entry.businessId);
    if (!covered) continue; // 阶段 6 已记录 IDENTIFIER_CONFLICT
    findings.push(...compareRecords(entry.mappedRecord, covered, approvalTable, executionTable, entry.businessId, executionDefaults));
  }

  const decision = findings.length ? DECISION.BLOCK : DECISION.ALLOW;
  return {
    decision,
    findings,
    ledger: receiptResult.ledger,
    effective: {
      approvalVersion: `${approvalTable.service}@${approvalTable.versionLabel}`,
      executionVersion: `${executionTable.service}@${executionTable.versionLabel}`,
      approval: approvalBatch.restored.map((e) => e.record),
      execution: mappedExecution.map((e) => e.mappedRecord),
      coverage: [...coverageResult.map.values()],
      appliedDefaults: {
        approval: approvalBatch.restored.flatMap((e) => e.appliedDefaults),
        execution: executionDefaults
      }
    },
    comparedAt: new Date().toISOString()
  };
}

export { DECISION };
