// 处置模块统一错误类型。所有可预期的拒绝 / 阻断都以 QuarantineError 抛出，
// 携带稳定的 code，便于上层（CLI、自动化编排）按码分支处理。
export class QuarantineError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'QuarantineError';
    this.code = code;
    this.details = details;
  }
}

// 输入的处置对象（检测结果）字段不完整或格式非法。
export class InvalidDetectionError extends QuarantineError {
  constructor(message, details) {
    super('INVALID_DETECTION', message, details);
  }
}

// 原位置文件内容相对检测时已变化，需要检测系统重新确认。
export class ReconfirmRequiredError extends QuarantineError {
  constructor(message, details) {
    super('RECONFIRM_REQUIRED', message, details);
  }
}

// 原位置已不存在，无法安全隔离 / 还原，需要人工介入。
export class MissingOriginalError extends QuarantineError {
  constructor(message, details) {
    super('MISSING_ORIGINAL', message, details);
  }
}

// 隔离副本缺失，处置状态无法继续推进。
export class MissingCopyError extends QuarantineError {
  constructor(message, details) {
    super('MISSING_COPY', message, details);
  }
}

// 隔离副本摘要与记录不一致，副本疑似损坏或被篡改。
export class CopyTamperedError extends QuarantineError {
  constructor(message, details) {
    super('COPY_TAMPERED', message, details);
  }
}

// 还原时原位置被其他对象占用且内容不同，已按冲突流程保留两份内容。
export class RestoreConflictError extends QuarantineError {
  constructor(message, details) {
    super('RESTORE_CONFLICT', message, details);
  }
}
