/**
 * 判定原因码。每个 deny 决策至少携带一个原因，原因码是稳定契约，
 * 供上游签发器与审计流水线按码检索（可追溯判定依据）。
 */
export const REASONS = Object.freeze({
  MEASUREMENT_NOT_FOUND: Object.freeze({
    code: 'MEASUREMENT_NOT_FOUND',
    message: '启动测量登记不存在，无法作为签发证据。'
  }),
  MEASUREMENT_NOT_VERIFIED: Object.freeze({
    code: 'MEASUREMENT_NOT_VERIFIED',
    message: '启动测量尚未通过验证，不能用于身份申请。'
  }),
  MEASUREMENT_JOB_MISMATCH: Object.freeze({
    code: 'MEASUREMENT_JOB_MISMATCH',
    message: '测量所属作业与申请作业不一致。'
  }),
  MEASUREMENT_INSTANCE_MISMATCH: Object.freeze({
    code: 'MEASUREMENT_INSTANCE_MISMATCH',
    message: '测量仅绑定原作业实例；调度重试产生的新实例必须提交自己的测量。'
  }),
  MEASUREMENT_NODE_MISMATCH: Object.freeze({
    code: 'MEASUREMENT_NODE_MISMATCH',
    message: '测量仅可在原节点使用，申请节点与测量节点不一致。'
  }),

  LEASE_NOT_FOUND: Object.freeze({
    code: 'LEASE_NOT_FOUND',
    message: '调度租约不存在。'
  }),
  LEASE_SCOPE_MISMATCH: Object.freeze({
    code: 'LEASE_SCOPE_MISMATCH',
    message: '租约的作业/实例/节点范围与申请不匹配。'
  }),
  LEASE_NOT_ACTIVE: Object.freeze({
    code: 'LEASE_NOT_ACTIVE',
    message: '租约已被替换或失效，当前不存在有效的调度租约。'
  }),
  LEASE_NOT_YET_VALID: Object.freeze({
    code: 'LEASE_NOT_YET_VALID',
    message: '租约尚未进入生效窗口。'
  }),
  LEASE_EXPIRED: Object.freeze({
    code: 'LEASE_EXPIRED',
    message: '租约已超出有效窗口。'
  }),

  IDENTITY_NOT_REGISTERED: Object.freeze({
    code: 'IDENTITY_NOT_REGISTERED',
    message: '服务身份未登记。'
  }),
  IMAGE_DIGEST_MISMATCH: Object.freeze({
    code: 'IMAGE_DIGEST_MISMATCH',
    message: '身份登记的镜像摘要与启动测量的镜像摘要不一致。'
  }),
  AUDIENCE_NOT_PERMITTED: Object.freeze({
    code: 'AUDIENCE_NOT_PERMITTED',
    message: '身份受众不在该服务身份登记允许的受众集合内。'
  }),

  PERMISSION_REVOKED: Object.freeze({
    code: 'PERMISSION_REVOKED',
    message: '发布许可已被撤销，后续签发必须阻断。'
  }),
  IDENTITY_REVOKED: Object.freeze({
    code: 'IDENTITY_REVOKED',
    message: '服务身份登记已被撤销。'
  }),
  AUDIENCE_REVOKED: Object.freeze({
    code: 'AUDIENCE_REVOKED',
    message: '该身份受众的发布许可已被撤销。'
  }),

  APPROVAL_NOT_FOUND: Object.freeze({
    code: 'APPROVAL_NOT_FOUND',
    message: '审批记录不存在，无法消费。'
  }),
  APPROVAL_ALREADY_CONSUMED: Object.freeze({
    code: 'APPROVAL_ALREADY_CONSUMED',
    message: '审批为一次性签发凭证，已被消费，不能重复使用。'
  }),
  APPROVAL_BINDING_MISMATCH: Object.freeze({
    code: 'APPROVAL_BINDING_MISMATCH',
    message: '消费请求与审批绑定的租约版本/身份受众不一致。'
  }),
  APPROVAL_EVIDENCE_STALE: Object.freeze({
    code: 'APPROVAL_EVIDENCE_STALE',
    message: '审批所依据的测量或身份登记已不再有效。'
  })
});

export function reason(code, detail) {
  const template = REASONS[code];
  if (!template) {
    throw new TypeError(`未知判定原因码: ${code}`);
  }
  return detail ? { ...template, detail } : { ...template };
}
