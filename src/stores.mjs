/**
 * 本地证据登记处。
 *
 * 全部为内存实现，写入/读取均做结构化拷贝，防止裁决依据被外部引用篡改；
 * 每个写操作记录时钟时间戳，形成可追溯的本地时间线。
 */

function clone(value) {
  return value === undefined ? undefined : structuredClone(value);
}

function requireString(value, field) {
  if (typeof value !== 'string' || value.length === 0) {
    throw new TypeError(`字段 ${field} 必须是非空字符串`);
  }
}

/** 已验证的启动测量登记（测量本身的验证由测量流水线完成，这里登记其结果）。 */
export class MeasurementRegistry {
  #byId = new Map();
  #clock;

  constructor(clock) {
    this.#clock = clock;
  }

  /**
   * @param {object} m
   * @param {string} m.measurementId
   * @param {string} m.jobId
   * @param {string} m.instanceId 作业实例（调度重试会产生新实例）
   * @param {string} m.nodeId     测量生成时所在节点
   * @param {string} m.imageDigest
   * @param {boolean} [m.verified=true] 测量流水线是否已验证通过
   */
  put(m) {
    requireString(m?.measurementId, 'measurementId');
    requireString(m.jobId, 'jobId');
    requireString(m.instanceId, 'instanceId');
    requireString(m.nodeId, 'nodeId');
    requireString(m.imageDigest, 'imageDigest');
    if (this.#byId.has(m.measurementId)) {
      throw new Error(`测量已登记: ${m.measurementId}`);
    }
    this.#byId.set(m.measurementId, {
      measurementId: m.measurementId,
      jobId: m.jobId,
      instanceId: m.instanceId,
      nodeId: m.nodeId,
      imageDigest: m.imageDigest,
      verified: m.verified !== false,
      recordedAt: this.#clock.now()
    });
  }

  get(measurementId) {
    return clone(this.#byId.get(measurementId));
  }
}

/** 调度租约登记，支持租约替换（版本递增、旧租约置位）。 */
export class LeaseRegistry {
  #byId = new Map();
  #clock;

  constructor(clock) {
    this.#clock = clock;
  }

  /**
   * @param {object} lease
   * @param {string} lease.leaseId
   * @param {number} lease.version 从 1 起单调递增
   * @param {string} lease.jobId
   * @param {string} lease.instanceId
   * @param {string} lease.nodeId
   * @param {string} [lease.replacesLeaseId] 本租约替换掉的旧租约
   * @param {string} [lease.validFrom] ISO 时间，缺省为时钟当前时间
   * @param {string} [lease.validUntil] ISO 时间，缺省表示不设上限
   * @param {string} [lease.status] 缺省 ACTIVE；可显式登记 REVOKED
   */
  put(lease) {
    requireString(lease?.leaseId, 'leaseId');
    requireString(lease.jobId, 'jobId');
    requireString(lease.instanceId, 'instanceId');
    requireString(lease.nodeId, 'nodeId');
    if (!Number.isInteger(lease.version) || lease.version < 1) {
      throw new TypeError('租约 version 必须是 >= 1 的整数');
    }
    if (this.#byId.has(lease.leaseId)) {
      throw new Error(`租约已登记: ${lease.leaseId}`);
    }
    this.#byId.set(lease.leaseId, {
      leaseId: lease.leaseId,
      version: lease.version,
      jobId: lease.jobId,
      instanceId: lease.instanceId,
      nodeId: lease.nodeId,
      replacesLeaseId: lease.replacesLeaseId ?? null,
      validFrom: lease.validFrom ?? this.#clock.now(),
      validUntil: lease.validUntil ?? null,
      status: lease.status ?? 'ACTIVE',
      recordedAt: this.#clock.now()
    });

    // 租约替换：旧租约立即失去 ACTIVE 状态。
    if (lease.replacesLeaseId) {
      const old = this.#byId.get(lease.replacesLeaseId);
      if (old) {
        old.status = 'REPLACED';
        old.replacedAt = this.#clock.now();
        old.replacedBy = lease.leaseId;
      }
    }
  }

  get(leaseId) {
    return clone(this.#byId.get(leaseId));
  }

  revoke(leaseId) {
    const lease = this.#byId.get(leaseId);
    if (!lease) {
      throw new Error(`租约不存在: ${leaseId}`);
    }
    lease.status = 'REVOKED';
    lease.revokedAt = this.#clock.now();
  }
}

/** 镜像摘要 -> 服务身份登记。 */
export class IdentityRegistry {
  #byName = new Map();
  #clock;

  constructor(clock) {
    this.#clock = clock;
  }

  /**
   * @param {object} reg
   * @param {string} reg.serviceIdentity 服务身份名，如 spiffe://cluster/ns/prod/sa/billing
   * @param {string} reg.imageDigest     该身份允许的镜像摘要
   * @param {string[]} reg.allowedAudiences 允许绑定的身份受众
   * @param {string[]} [reg.permissions] 已发布许可集合
   * @param {boolean} [reg.active=true]
   */
  put(reg) {
    requireString(reg?.serviceIdentity, 'serviceIdentity');
    requireString(reg.imageDigest, 'imageDigest');
    if (!Array.isArray(reg.allowedAudiences) || reg.allowedAudiences.length === 0) {
      throw new TypeError('allowedAudiences 必须是非空数组');
    }
    if (this.#byName.has(reg.serviceIdentity)) {
      throw new Error(`身份已登记: ${reg.serviceIdentity}`);
    }
    this.#byName.set(reg.serviceIdentity, {
      serviceIdentity: reg.serviceIdentity,
      imageDigest: reg.imageDigest,
      allowedAudiences: [...reg.allowedAudiences],
      permissions: [...(reg.permissions ?? [])],
      active: reg.active !== false,
      registeredAt: this.#clock.now()
    });
  }

  get(serviceIdentity) {
    return clone(this.#byName.get(serviceIdentity));
  }
}

/**
 * 发布许可撤销登记（事件流）。
 * scope:
 *  - { scope: 'PERMISSION', serviceIdentity, permission } 单个发布许可撤销
 *  - { scope: 'AUDIENCE',  serviceIdentity, audience }   某身份对某受众的许可撤销
 *  - { scope: 'IDENTITY',  serviceIdentity }             整个服务身份撤销
 */
export class RevocationRegistry {
  #events = [];
  #clock;

  constructor(clock) {
    this.#clock = clock;
  }

  publish(event) {
    requireString(event?.scope, 'scope');
    requireString(event.serviceIdentity, 'serviceIdentity');
    if (event.scope === 'PERMISSION') requireString(event.permission, 'permission');
    if (event.scope === 'AUDIENCE') requireString(event.audience, 'audience');
    if (!['PERMISSION', 'AUDIENCE', 'IDENTITY'].includes(event.scope)) {
      throw new TypeError(`非法撤销范围: ${event.scope}`);
    }
    const stored = {
      revocationId: `rev-${this.#events.length + 1}`,
      scope: event.scope,
      serviceIdentity: event.serviceIdentity,
      permission: event.permission ?? null,
      audience: event.audience ?? null,
      reason: event.reason ?? null,
      revokedAt: this.#clock.now(),
      seq: this.#clock.next()
    };
    this.#events.push(stored);
    return clone(stored);
  }

  list() {
    return clone(this.#events);
  }

  /** 返回与 (身份, 受众, 许可集合) 匹配的最新撤销事件，无匹配返回 null。 */
  findActive({ serviceIdentity, audience, permissions }) {
    for (let i = this.#events.length - 1; i >= 0; i -= 1) {
      const ev = this.#events[i];
      if (ev.serviceIdentity !== serviceIdentity) continue;
      if (ev.scope === 'IDENTITY') return clone(ev);
      if (ev.scope === 'AUDIENCE' && ev.audience === audience) return clone(ev);
      if (ev.scope === 'PERMISSION' && permissions.includes(ev.permission)) {
        return clone(ev);
      }
    }
    return null;
  }
}

/** 审批凭证登记（一次性消费）。 */
export class ApprovalStore {
  #byId = new Map();
  #clock;

  constructor(clock) {
    this.#clock = clock;
  }

  save(approval) {
    if (this.#byId.has(approval.approvalId)) {
      throw new Error(`审批已存在: ${approval.approvalId}`);
    }
    this.#byId.set(approval.approvalId, { ...clone(approval), recordedAt: this.#clock.now() });
  }

  get(approvalId) {
    return clone(this.#byId.get(approvalId));
  }

  markConsumed(approvalId, credentialId) {
    const stored = this.#byId.get(approvalId);
    if (!stored) throw new Error(`审批不存在: ${approvalId}`);
    stored.consumed = true;
    stored.consumedAt = this.#clock.now();
    stored.credentialId = credentialId;
  }
}

/** 决策审计日志：所有裁决（allow/deny）均不可变落账。 */
export class DecisionLog {
  #entries = [];
  #clock;

  constructor(clock) {
    this.#clock = clock;
  }

  append(entry) {
    const stored = clone(entry);
    stored.loggedAt = this.#clock.now();
    this.#entries.push(stored);
    return clone(stored);
  }

  list() {
    return clone(this.#entries);
  }

  findByDecisionId(decisionId) {
    return clone(this.#entries.find((e) => e.decisionId === decisionId));
  }

  findByRequestId(requestId) {
    return clone(this.#entries.filter((e) => e.requestId === requestId));
  }
}
