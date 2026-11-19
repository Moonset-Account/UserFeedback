// 裁决模块的领域错误类型，便于调用方按错误类别处理。

export class AdjudicationError extends Error {
  constructor(message) {
    super(message);
    this.name = 'AdjudicationError';
  }
}

/** 存证阶段已结束（揭示窗口开启或已过期）后再提交存证 */
export class DepositWindowClosedError extends AdjudicationError {
  constructor(message = '存证阶段已结束，不再接收新存证') {
    super(message);
    this.name = 'DepositWindowClosedError';
  }
}

/** 揭示窗口未开启 */
export class RevealNotOpenError extends AdjudicationError {
  constructor(message = '揭示窗口尚未开启') {
    super(message);
    this.name = 'RevealNotOpenError';
  }
}

/** 揭示窗口已关闭（晚揭示，存证失去资格且不入库） */
export class RevealWindowClosedError extends AdjudicationError {
  constructor(message = '揭示窗口已关闭，该揭示不予受理') {
    super(message);
    this.name = 'RevealWindowClosedError';
  }
}

/** 揭示内容重算摘要与存证摘要不匹配 */
export class CommitmentMismatchError extends AdjudicationError {
  constructor(message = '揭示内容与存证摘要不匹配') {
    super(message);
    this.name = 'CommitmentMismatchError';
  }
}

/** 同一存证重复揭示 */
export class DepositAlreadyRevealedError extends AdjudicationError {
  constructor(message = '该存证已完成揭示') {
    super(message);
    this.name = 'DepositAlreadyRevealedError';
  }
}

/** 引用了不存在的存证标识 */
export class UnknownDepositError extends AdjudicationError {
  constructor(message = '存证标识不存在') {
    super(message);
    this.name = 'UnknownDepositError';
  }
}
