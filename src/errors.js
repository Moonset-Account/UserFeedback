/**
 * 调用方传入的门禁输入本身结构不合法（编程错误），抛出此异常。
 * 业务上“应被阻止”的结果不抛异常，而是以 decision=BLOCK + findings 返回。
 */
export class GateInputError extends Error {
  constructor(message, details) {
    super(message);
    this.name = 'GateInputError';
    if (details !== undefined) this.details = details;
  }
}

/** 阻断判定原因码，集中维护，避免魔法字符串散落在比较逻辑中。 */
export const ReasonCode = Object.freeze({
  // 回执 / 版本时效
  RECEIPT_DIGEST_MISMATCH: 'RECEIPT_DIGEST_MISMATCH',
  RECEIPT_UNKNOWN_VERSION: 'RECEIPT_UNKNOWN_VERSION',
  RECEIPT_FUTURE_VERSION: 'RECEIPT_FUTURE_VERSION',
  RECEIPT_STALE: 'RECEIPT_STALE',
  RECEIPT_DUPLICATE: 'RECEIPT_DUPLICATE',
  RECEIPT_MISSING: 'RECEIPT_MISSING',

  // 语义还原
  TYPE_VIOLATION: 'TYPE_VIOLATION',
  MISSING_FIELD: 'MISSING_FIELD',
  UNDECLARED_FIELD: 'UNDECLARED_FIELD',
  DEFAULT_CYCLE: 'DEFAULT_CYCLE',
  ENUM_UNMAPPED: 'ENUM_UNMAPPED',

  // 清单对齐
  DUPLICATE_IDENTIFIER: 'DUPLICATE_IDENTIFIER',
  IDENTIFIER_CONFLICT: 'IDENTIFIER_CONFLICT',
  IDENTIFIER_MISALIGNED: 'IDENTIFIER_MISALIGNED',

  // 与审批一致性
  COVERAGE_MISMATCH: 'COVERAGE_MISMATCH',
  COVERAGE_INCOMPLETE: 'COVERAGE_INCOMPLETE',
  OP_MISMATCH: 'OP_MISMATCH',
  TARGET_MISMATCH: 'TARGET_MISMATCH',
  SECURITY_UNMAPPED: 'SECURITY_UNMAPPED',
  SECURITY_MISMATCH: 'SECURITY_MISMATCH',
  SCOPE_WIDENED: 'SCOPE_WIDENED',
  DEFAULT_EXPANDED: 'DEFAULT_EXPANDED',
  PARAM_MISMATCH: 'PARAM_MISMATCH'
});

/** 来源侧定位，配合 finding.location 精确定位差异来源。 */
export const Source = Object.freeze({
  APPROVAL_PARSE: 'approval-parse',
  EXECUTION_PARSE: 'execution-parse',
  APPROVAL_COVERAGE: 'approval-coverage',
  GATE: 'gate'
});
