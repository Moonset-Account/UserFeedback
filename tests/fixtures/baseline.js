/**
 * 合成夹具：审批服务（approval-svc）与执行服务（execution-svc）分批升级。
 * 审批侧当前 v2（保留 v1 历史表），执行侧当前 v3（保留 v2 历史表）。
 * 所有数据均为本地虚构，不对应任何真实目标。
 */

export const DIGEST = 'sha256:req-7f3a9c21';

export const APPROVAL_SERVICE = 'approval-svc';
export const EXECUTION_SERVICE = 'execution-svc';

// ---- 审批服务语义表：v1（历史） -> v2（当前） ----
export const approvalV1 = {
  service: APPROVAL_SERVICE,
  versionLabel: 'v1',
  versionOrdinal: 1,
  fields: [
    { path: 'id', type: 'string', role: 'identifier', required: true },
    { path: 'action', type: 'enum', role: 'op', enum: ['read', 'write', 'delete'], required: true },
    { path: 'target.resourceId', type: 'string', role: 'target', required: true },
    { path: 'target.region', type: 'string', role: 'target', required: true },
    { path: 'permissions', type: 'set', role: 'scope', enum: ['read', 'write', 'admin'], required: true },
    { path: 'encryption', type: 'enum', role: 'security', enum: ['AES256', 'NONE'] },
    { path: 'retentionDays', type: 'integer', role: 'info' }
  ]
};

export const approvalV2 = {
  service: APPROVAL_SERVICE,
  versionLabel: 'v2',
  versionOrdinal: 2,
  fields: [
    { path: 'id', type: 'string', role: 'identifier', required: true },
    {
      path: 'action',
      type: 'enum',
      role: 'op',
      enum: ['read', 'write', 'delete'],
      required: true
    },
    { path: 'target.resourceId', type: 'string', role: 'target', required: true },
    { path: 'target.region', type: 'string', role: 'target', required: true },
    {
      path: 'permissions',
      type: 'set',
      role: 'scope',
      enum: ['read', 'write', 'admin'],
      required: true,
      defaults: [
        // 无条件兜底默认值在审批侧同样只赋予最小权限，不扩大审批范围
        { value: ['read'], reason: 'approval-scope-fallback' }
      ]
    },
    {
      path: 'encryption',
      type: 'enum',
      role: 'security',
      enum: ['AES256', 'NONE'],
      defaults: [
        // 条件默认值：删除操作必须加密
        { when: { field: 'action', equals: 'delete' }, value: 'AES256', reason: 'delete-requires-encryption' },
        { value: 'NONE', reason: 'encryption-fallback' }
      ]
    },
    { path: 'retentionDays', type: 'integer', role: 'info' }
  ]
};

// ---- 执行服务语义表：v2（历史） -> v3（当前） ----
export const executionV2 = {
  service: EXECUTION_SERVICE,
  versionLabel: 'v2',
  versionOrdinal: 2,
  fields: [
    { path: 'id', type: 'string', role: 'identifier', required: true },
    { path: 'action', type: 'enum', role: 'op', enum: ['read', 'write', 'delete'], required: true },
    { path: 'target.resourceId', type: 'string', role: 'target', required: true },
    { path: 'target.region', type: 'string', role: 'target', required: true },
    { path: 'permissions', type: 'set', role: 'scope', enum: ['read', 'write', 'admin'], required: true },
    { path: 'encryption', type: 'enum', role: 'security', enum: ['AES256', 'NONE'] },
    { path: 'retentionDays', type: 'integer', role: 'info' }
  ]
};

export const executionV3 = {
  service: EXECUTION_SERVICE,
  versionLabel: 'v3',
  versionOrdinal: 3,
  fields: [
    { path: 'id', type: 'string', role: 'identifier', required: true },
    {
      path: 'action',
      type: 'enum',
      role: 'op',
      enum: ['read', 'write', 'delete'],
      required: true,
      mappings: [
        { targetService: APPROVAL_SERVICE, targetVersion: 'v2', values: { read: 'read', write: 'write', delete: 'delete' } }
      ]
    },
    { path: 'target.resourceId', type: 'string', role: 'target', required: true },
    { path: 'target.region', type: 'string', role: 'target', required: true },
    {
      path: 'permissions',
      type: 'set',
      role: 'scope',
      enum: ['read', 'write', 'admin'],
      required: true,
      // 条件默认值：按操作推断最小权限（不超过显式审批范围时才可放行）
      defaults: [
        { when: { field: 'action', equals: 'read' }, value: ['read'], reason: 'read-implies-read-only' },
        { when: { field: 'action', equals: 'write' }, value: ['read', 'write'], reason: 'write-implies-read-write' },
        { when: { field: 'action', equals: 'delete' }, value: ['admin'], reason: 'delete-implies-admin' }
      ],
      mappings: [
        {
          targetService: APPROVAL_SERVICE,
          targetVersion: 'v2',
          values: { read: 'read', write: 'write', admin: 'admin' }
        }
      ]
    },
    {
      // v3 换了安全词汇表：AES-256-GCM / DISABLED，需跨版本映射
      path: 'encryption',
      type: 'enum',
      role: 'security',
      enum: ['AES-256-GCM', 'DISABLED'],
      defaults: [
        { when: { field: 'action', equals: 'delete' }, value: 'AES-256-GCM', reason: 'delete-requires-encryption' },
        { value: 'DISABLED', reason: 'encryption-fallback' }
      ],
      mappings: [
        {
          targetService: APPROVAL_SERVICE,
          targetVersion: 'v2',
          values: { 'AES-256-GCM': 'AES256', DISABLED: 'NONE' }
        }
      ]
    },
    {
      path: 'retentionDays',
      type: 'integer',
      role: 'info',
      defaults: [{ value: 30, reason: 'platform-default-retention' }]
    }
  ]
};

export const semanticTables = {
  [APPROVAL_SERVICE]: [approvalV1, approvalV2],
  [EXECUTION_SERVICE]: [executionV2, executionV3]
};

// ---- 审批覆盖参数（审批所覆盖的参数，基准场景） ----
export const baselineCoverage = [
  {
    id: 'op-1',
    action: 'write',
    target: { resourceId: 'bucket-a/object-1', region: 'cn-north-1' },
    permissions: ['read', 'write'],
    encryption: 'AES256',
    retentionDays: 30
  },
  {
    id: 'op-2',
    action: 'read',
    target: { resourceId: 'bucket-a/object-2', region: 'cn-north-1' },
    permissions: ['read'],
    encryption: 'NONE',
    retentionDays: 30
  }
];

// 审批侧解析结果：与覆盖参数完全一致（基准场景）
export const baselineApprovalParse = structuredClone(baselineCoverage);

// 执行侧 v3 解析结果：
// - op-1 显式给出收窄后的只读范围 + 新词汇表的安全选项；
// - op-2 缺 permissions/encryption/retentionDays，由条件默认值还原（收窄，不扩权）。
export const baselineExecutionParse = [
  {
    id: 'op-1',
    action: 'write',
    target: { resourceId: 'bucket-a/object-1', region: 'cn-north-1' },
    permissions: ['read'],
    encryption: 'AES-256-GCM',
    retentionDays: 30
  },
  {
    id: 'op-2',
    action: 'read',
    target: { resourceId: 'bucket-a/object-2', region: 'cn-north-1' }
  }
];

export const baselineReceipts = [
  {
    service: APPROVAL_SERVICE,
    digest: DIGEST,
    versionLabel: 'v2',
    sequence: 1,
    receivedAt: '2026-11-19T10:00:00.000Z'
  },
  {
    service: EXECUTION_SERVICE,
    digest: DIGEST,
    versionLabel: 'v3',
    sequence: 1,
    receivedAt: '2026-11-19T10:00:01.000Z'
  }
];

/** 构造一次门禁调用输入，深拷贝保证用例之间互不污染。 */
export function buildGateInput(overrides = {}) {
  return {
    digest: DIGEST,
    semanticTables,
    approval: {
      service: APPROVAL_SERVICE,
      parse: structuredClone(baselineApprovalParse),
      coverage: structuredClone(baselineCoverage)
    },
    execution: {
      service: EXECUTION_SERVICE,
      parse: structuredClone(baselineExecutionParse)
    },
    receipts: structuredClone(baselineReceipts),
    ledger: {},
    ...overrides
  };
}

export function clone(value) {
  return structuredClone(value);
}
