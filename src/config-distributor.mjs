// 本地机密配置发放模块
//
// 核心约束：
//  - 配置按不可变版本发布；普通发布新版本不会关闭旧版本上已发出的有效读取租约。
//  - 紧急撤销把指定版本标记为 revoked，该版本的一切读取立即停止发放。
//  - 租约绑定 (实例标识, 进程代次, 版本, 到期时刻)；实例以更新的代次重新登记后，
//    旧代次租约一律失效，重启后不可复用。
//  - 读取在单一临界区内快照一个完整版本，版本切换与读取交错时不会出现跨版本字段拼接。
//  - 实例只得到其批准字段且本次请求字段的子集；任一字段未批准即拒绝整次读取。
//  - 审计条目仅包含 version 与 decision 两个键，绝不记录字段名、字段值、实例或租约标识。

import { randomUUID } from 'node:crypto';

export const DENY_REASONS = Object.freeze({
  UNKNOWN_LEASE: 'unknown-lease',
  GENERATION_MISMATCH: 'generation-mismatch',
  LEASE_EXPIRED: 'lease-expired',
  VERSION_REVOKED: 'version-revoked',
  INSTANCE_UNKNOWN: 'instance-unknown',
  FIELD_NOT_APPROVED: 'field-not-approved',
});

export class DistributorError extends Error {
  constructor(reason, message) {
    super(message ?? reason);
    this.name = 'DistributorError';
    this.reason = reason;
  }
}

function isNonEmptyString(value) {
  return typeof value === 'string' && value.length > 0;
}

function deepFreeze(value) {
  if (value === null || typeof value !== 'object' || Object.isFrozen(value)) {
    return value;
  }
  for (const key of Reflect.ownKeys(value)) {
    deepFreeze(value[key]);
  }
  return Object.freeze(value);
}

async function defaultBarrier() {
  // 默认无注入点：真实运行时不产生额外让出。
}

export class ConfigDistributor {
  #now;
  #defaultTtlMs;
  #versions = new Map(); // version -> { version, fields, revoked }
  #currentVersion = null;
  #instances = new Map(); // instanceId -> { instanceId, generation, approvedFields:Set }
  #leases = new Map(); // leaseId -> { leaseId, instanceId, generation, version, expiresAt, superseded }
  #audit = [];

  constructor({ now = () => Date.now(), defaultTtlMs = 60_000 } = {}) {
    if (typeof now !== 'function') {
      throw new TypeError('now must be a function');
    }
    if (!Number.isInteger(defaultTtlMs) || defaultTtlMs <= 0) {
      throw new TypeError('defaultTtlMs must be a positive integer');
    }
    this.#now = now;
    this.#defaultTtlMs = defaultTtlMs;
  }

  // ---- 版本管理 -------------------------------------------------------------

  publishVersion(version, fields) {
    if (!isNonEmptyString(version)) {
      throw new TypeError('version must be a non-empty string');
    }
    if (fields === null || typeof fields !== 'object' || Array.isArray(fields)) {
      throw new TypeError('fields must be a plain object');
    }
    if (this.#versions.has(version)) {
      throw new DistributorError(
        'version-exists',
        `version ${version} is already published and is immutable`,
      );
    }
    const keys = Object.keys(fields);
    if (keys.length === 0) {
      throw new TypeError('fields must contain at least one field');
    }
    for (const key of keys) {
      if (!isNonEmptyString(key)) {
        throw new TypeError('field names must be non-empty strings');
      }
    }
    // 深拷贝后冻结：发布后版本内容不可变，调用方后续改动无法影响已发布版本。
    const snapshot = deepFreeze(structuredClone(fields));
    const record = { version, fields: snapshot, revoked: false };
    this.#versions.set(version, record);
    this.#currentVersion = version;
    return this.#versionView(record);
  }

  revokeVersion(version) {
    const record = this.#versions.get(version);
    if (record === undefined) {
      throw new DistributorError('version-unknown', `unknown version ${version}`);
    }
    record.revoked = true; // 已发出的租约读取时会重新检查该标记
    return { version, revoked: true };
  }

  getCurrentVersion() {
    return this.#currentVersion;
  }

  isVersionRevoked(version) {
    return this.#versions.get(version)?.revoked ?? false;
  }

  // ---- 实例登记 -------------------------------------------------------------

  registerInstance({ instanceId, generation, approvedFields }) {
    if (!isNonEmptyString(instanceId)) {
      throw new TypeError('instanceId must be a non-empty string');
    }
    if (!Number.isInteger(generation) || generation < 0) {
      throw new TypeError('generation must be a non-negative integer');
    }
    if (!Array.isArray(approvedFields) || approvedFields.some((f) => !isNonEmptyString(f))) {
      throw new TypeError('approvedFields must be an array of non-empty strings');
    }
    const previous = this.#instances.get(instanceId);
    if (previous !== undefined && generation <= previous.generation) {
      throw new DistributorError(
        'generation-not-advanced',
        `generation must advance beyond ${previous.generation} for instance ${instanceId}`,
      );
    }
    const record = {
      instanceId,
      generation,
      approvedFields: new Set(approvedFields),
    };
    this.#instances.set(instanceId, record);
    // 同一实例重启（代次推进）：旧代次租约立即作废，不可复用。
    for (const lease of this.#leases.values()) {
      if (lease.instanceId === instanceId && lease.generation !== generation) {
        lease.superseded = true;
      }
    }
    return this.#instanceView(record);
  }

  getInstance(instanceId) {
    const record = this.#instances.get(instanceId);
    return record === undefined ? null : this.#instanceView(record);
  }

  // ---- 读取租约 -------------------------------------------------------------

  async issueLease({ instanceId, generation, ttlMs } = {}, barrier = defaultBarrier) {
    if (typeof barrier !== 'function') {
      throw new TypeError('barrier must be a function');
    }
    const instance = this.#instances.get(instanceId);
    if (instance === undefined) {
      throw new DistributorError(DENY_REASONS.INSTANCE_UNKNOWN);
    }
    if (instance.generation !== generation) {
      throw new DistributorError(DENY_REASONS.GENERATION_MISMATCH);
    }
    const ttl = ttlMs ?? this.#defaultTtlMs;
    if (!Number.isInteger(ttl) || ttl <= 0) {
      throw new TypeError('ttlMs must be a positive integer');
    }

    // 在让出事件循环之前捕获当前版本指针，保证租约钉住一个确定版本。
    const pinned = this.#currentRecord();
    if (pinned.revoked) {
      throw new DistributorError(DENY_REASONS.VERSION_REVOKED);
    }

    await barrier('version-captured', { instanceId, version: pinned.version });

    // 交错点之后重新确认：钉住的版本可能在等待期间被紧急撤销。
    if (pinned.revoked) {
      throw new DistributorError(DENY_REASONS.VERSION_REVOKED);
    }

    const now = this.#now();
    const lease = {
      leaseId: randomUUID(),
      instanceId,
      generation,
      version: pinned.version,
      expiresAt: now + ttl,
      superseded: false,
    };
    this.#leases.set(lease.leaseId, lease);
    return this.#leaseView(lease);
  }

  // ---- 配置读取 -------------------------------------------------------------

  async readConfig({ leaseId, fields } = {}, barrier = defaultBarrier) {
    if (typeof barrier !== 'function') {
      throw new TypeError('barrier must be a function');
    }
    if (!Array.isArray(fields) || fields.length === 0 || fields.some((f) => !isNonEmptyString(f))) {
      throw new TypeError('fields must be a non-empty array of non-empty strings');
    }
    if (new Set(fields).size !== fields.length) {
      throw new TypeError('fields must not contain duplicates');
    }

    const denied = (version, reason) => {
      const entry = Object.freeze({ version, decision: 'denied' });
      this.#audit.push(entry);
      return { granted: false, version, reason };
    };

    const lease = this.#leases.get(leaseId);
    if (lease === undefined) {
      return denied(null, DENY_REASONS.UNKNOWN_LEASE);
    }

    const instance = this.#instances.get(lease.instanceId);
    if (lease.superseded || instance === undefined || instance.generation !== lease.generation) {
      return denied(lease.version, DENY_REASONS.GENERATION_MISMATCH);
    }
    if (this.#now() >= lease.expiresAt) {
      return denied(lease.version, DENY_REASONS.LEASE_EXPIRED);
    }

    // 交错注入点：等待期间可能发生版本发布、紧急撤销或时钟推进。
    await barrier('lease-validated', { leaseId, version: lease.version });

    // 等待后重新评估全部停止条件，避免基于陈旧状态发放。
    if (lease.superseded || instance.generation !== this.#instances.get(lease.instanceId)?.generation) {
      return denied(lease.version, DENY_REASONS.GENERATION_MISMATCH);
    }
    if (this.#now() >= lease.expiresAt) {
      return denied(lease.version, DENY_REASONS.LEASE_EXPIRED);
    }

    // 一次性快照租约钉住的完整不可变版本；读取结果只可能来自这一个版本。
    const record = this.#versions.get(lease.version);
    if (record === undefined || record.revoked) {
      return denied(lease.version, DENY_REASONS.VERSION_REVOKED);
    }

    await barrier('snapshot-captured', { leaseId, version: record.version });

    if (record.revoked) {
      return denied(record.version, DENY_REASONS.VERSION_REVOKED);
    }

    // 部分字段授权：仅发放“请求 ∩ 批准 ∩ 版本存在”的子集，缺一即整次拒绝。
    for (const field of fields) {
      if (!instance.approvedFields.has(field) || !Object.hasOwn(record.fields, field)) {
        return denied(record.version, DENY_REASONS.FIELD_NOT_APPROVED);
      }
    }

    const config = {};
    for (const field of fields) {
      config[field] = record.fields[field];
    }
    Object.freeze(config);

    this.#audit.push(Object.freeze({ version: record.version, decision: 'granted' }));
    return { granted: true, version: record.version, config };
  }

  // ---- 审计 -----------------------------------------------------------------

  getAuditLog() {
    // 返回副本；条目本身已冻结，外部无法改写历史。
    return this.#audit.slice();
  }

  // ---- 内部辅助 -------------------------------------------------------------

  #currentRecord() {
    if (this.#currentVersion === null) {
      throw new DistributorError('no-version-published', 'no configuration version has been published');
    }
    return this.#versions.get(this.#currentVersion);
  }

  #versionView(record) {
    return Object.freeze({ version: record.version, fields: record.fields, revoked: record.revoked });
  }

  #instanceView(record) {
    return Object.freeze({
      instanceId: record.instanceId,
      generation: record.generation,
      approvedFields: Object.freeze([...record.approvedFields]),
    });
  }

  #leaseView(lease) {
    return Object.freeze({
      leaseId: lease.leaseId,
      instanceId: lease.instanceId,
      generation: lease.generation,
      version: lease.version,
      expiresAt: lease.expiresAt,
    });
  }
}
