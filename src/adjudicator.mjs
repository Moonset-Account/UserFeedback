import { reason } from './reasons.mjs';

/**
 * 工作负载身份裁决模块。
 *
 * 两阶段模型：
 *   1. requestApproval：读取【已验证启动测量 + 调度租约 + 镜像摘要对应的服务身份登记】，
 *      做交叉约束裁决；通过则生成一次性审批，并把【租约版本 + 身份受众】绑定进审批。
 *   2. issue：消费审批时重新校验活证据（租约是否被替换/过期、许可是否被撤销），
 *      旧审批在租约替换或撤销后一律不可消费。
 *
 * 每个裁决（allow/deny）都带稳定原因码、证据快照与绑定事实，写入不可变决策日志。
 */
export class Adjudicator {
  #clock;
  #measurements;
  #leases;
  #identities;
  #revocations;
  #approvals;
  #decisions;

  constructor({ clock, measurements, leases, identities, revocations, approvals, decisions }) {
    this.#clock = clock;
    this.#measurements = measurements;
    this.#leases = leases;
    this.#identities = identities;
    this.#revocations = revocations;
    this.#approvals = approvals;
    this.#decisions = decisions;
  }

  // ---------------------------------------------------------------- 阶段一：审批

  /**
   * @param {object} req 签发请求（审批阶段）
   * @returns {object} 决策对象；verdict=ALLOW 时带 approvalId
   */
  requestApproval(req) {
    this.#requireApprovalRequest(req);
    const context = { stage: 'APPROVAL', requestId: req.requestId };

    // ---- 1. 启动测量：仅原节点上的原作业实例可用 ----
    const measurement = this.#measurements.get(req.measurementId);
    if (!measurement) {
      return this.#deny(context, 'MEASUREMENT_NOT_FOUND', { requestedMeasurementId: req.measurementId });
    }
    if (!measurement.verified) {
      return this.#deny(context, 'MEASUREMENT_NOT_VERIFIED', { measurement });
    }
    if (measurement.jobId !== req.jobId) {
      return this.#deny(context, 'MEASUREMENT_JOB_MISMATCH', {
        measurement, requested: { jobId: req.jobId }
      });
    }
    if (measurement.instanceId !== req.instanceId) {
      return this.#deny(context, 'MEASUREMENT_INSTANCE_MISMATCH', {
        measurement, requested: { instanceId: req.instanceId },
        note: '调度重试生成的新实例必须提供自己的测量。'
      });
    }
    if (measurement.nodeId !== req.nodeId) {
      return this.#deny(context, 'MEASUREMENT_NODE_MISMATCH', {
        measurement, requested: { nodeId: req.nodeId }
      });
    }

    // ---- 2. 调度租约：范围、状态、时间窗 ----
    const lease = this.#leases.get(req.leaseId);
    if (!lease) {
      return this.#deny(context, 'LEASE_NOT_FOUND', { requestedLeaseId: req.leaseId });
    }
    if (lease.jobId !== req.jobId || lease.instanceId !== req.instanceId || lease.nodeId !== req.nodeId) {
      return this.#deny(context, 'LEASE_SCOPE_MISMATCH', {
        lease, requested: { jobId: req.jobId, instanceId: req.instanceId, nodeId: req.nodeId }
      });
    }
    if (lease.status !== 'ACTIVE') {
      return this.#deny(context, 'LEASE_NOT_ACTIVE', { lease });
    }
    const windowProblem = this.#leaseWindowProblem(lease);
    if (windowProblem) {
      return this.#deny(context, windowProblem, { lease, evaluatedAt: this.#clock.now() });
    }

    // ---- 3. 服务身份登记：镜像摘要对应、受众允许 ----
    const identity = this.#identities.get(req.serviceIdentity);
    if (!identity) {
      return this.#deny(context, 'IDENTITY_NOT_REGISTERED', {
        requestedServiceIdentity: req.serviceIdentity
      });
    }
    if (!identity.active) {
      return this.#deny(context, 'IDENTITY_REVOKED', { identity });
    }
    if (identity.imageDigest !== measurement.imageDigest) {
      return this.#deny(context, 'IMAGE_DIGEST_MISMATCH', {
        identityDigest: identity.imageDigest,
        measurementDigest: measurement.imageDigest
      });
    }
    if (!identity.allowedAudiences.includes(req.audience)) {
      return this.#deny(context, 'AUDIENCE_NOT_PERMITTED', {
        audience: req.audience, allowedAudiences: identity.allowedAudiences
      });
    }

    // ---- 4. 发布许可撤销：撤销必须阻断随后续发 ----
    const revocation = this.#revocations.findActive({
      serviceIdentity: req.serviceIdentity,
      audience: req.audience,
      permissions: req.permissions
    });
    if (revocation) {
      return this.#deny(context, this.#revocationReason(revocation.scope), {
        revocation, requestedPermissions: req.permissions
      });
    }

    // ---- 通过：生成一次性审批，绑定租约版本与身份受众 ----
    const seq = this.#clock.next();
    const approval = {
      approvalId: `apv-${seq}`,
      requestId: req.requestId,
      serviceIdentity: req.serviceIdentity,
      jobId: req.jobId,
      instanceId: req.instanceId,
      nodeId: req.nodeId,
      measurementId: req.measurementId,
      leaseId: lease.leaseId,
      leaseVersion: lease.version,
      audience: req.audience,
      permissions: [...req.permissions],
      bindings: { leaseVersion: lease.version, audience: req.audience },
      approvedAt: this.#clock.now(),
      consumed: false
    };
    this.#approvals.save(approval);

    return this.#allow(context, {
      approvalId: approval.approvalId,
      basis: this.#basis(measurement, lease, identity, approval),
      evidence: { measurement, lease, identity, revocation: null }
    });
  }

  // ---------------------------------------------------------------- 阶段二：消费签发

  /**
   * 消费审批并签发身份凭证。消费请求必须重申绑定的租约版本与受众，
   * 且审批依据的租约/测量/身份/许可在签发时刻仍然有效。
   *
   * @param {object} consume { approvalId, requestId, leaseVersion, audience }
   */
  issue(consume) {
    if (!consume || typeof consume.approvalId !== 'string' || consume.approvalId.length === 0) {
      throw new TypeError('consume.approvalId 必须是非空字符串');
    }
    const context = { stage: 'ISSUANCE', requestId: consume.requestId ?? null };

    const approval = this.#approvals.get(consume.approvalId);
    if (!approval) {
      return this.#deny(context, 'APPROVAL_NOT_FOUND', { requestedApprovalId: consume.approvalId });
    }
    if (approval.consumed) {
      return this.#deny(context, 'APPROVAL_ALREADY_CONSUMED', {
        approvalId: approval.approvalId,
        consumedAt: approval.consumedAt,
        credentialId: approval.credentialId
      });
    }

    // 绑定校验：租约版本 + 受众必须与审批一致
    if (consume.leaseVersion !== undefined && consume.leaseVersion !== approval.leaseVersion) {
      return this.#deny(context, 'APPROVAL_BINDING_MISMATCH', {
        field: 'leaseVersion', approved: approval.leaseVersion, supplied: consume.leaseVersion
      });
    }
    if (consume.audience !== undefined && consume.audience !== approval.audience) {
      return this.#deny(context, 'APPROVAL_BINDING_MISMATCH', {
        field: 'audience', approved: approval.audience, supplied: consume.audience
      });
    }

    // 活证据复验：租约（替换/撤销/过期都会在此拦截旧审批）
    const lease = this.#leases.get(approval.leaseId);
    if (!lease) {
      return this.#deny(context, 'LEASE_NOT_FOUND', { approval });
    }
    if (lease.status !== 'ACTIVE') {
      return this.#deny(context, 'LEASE_NOT_ACTIVE', { lease, approval });
    }
    const windowProblem = this.#leaseWindowProblem(lease);
    if (windowProblem) {
      return this.#deny(context, windowProblem, { lease, approval });
    }
    // 租约版本若被原地提升（同一 leaseId 换新版本），同样按绑定失配拦截
    if (lease.version !== approval.leaseVersion) {
      return this.#deny(context, 'APPROVAL_BINDING_MISMATCH', {
        field: 'leaseVersion', approved: approval.leaseVersion, live: lease.version
      });
    }

    // 测量与身份登记复验
    const measurement = this.#measurements.get(approval.measurementId);
    const identity = this.#identities.get(approval.serviceIdentity);
    if (!measurement || !measurement.verified) {
      return this.#deny(context, 'APPROVAL_EVIDENCE_STALE', { field: 'measurement', measurement });
    }
    if (!identity || !identity.active || identity.imageDigest !== measurement.imageDigest) {
      return this.#deny(context, 'APPROVAL_EVIDENCE_STALE', { field: 'identity', identity });
    }

    // 撤销复验：审批之后发布的撤销同样阻断本次签发
    const revocation = this.#revocations.findActive({
      serviceIdentity: approval.serviceIdentity,
      audience: approval.audience,
      permissions: approval.permissions
    });
    if (revocation) {
      return this.#deny(context, this.#revocationReason(revocation.scope), {
        revocation, approval
      });
    }

    const seq = this.#clock.next();
    const credentialId = `cred-${seq}`;
    this.#approvals.markConsumed(approval.approvalId, credentialId);

    const credential = {
      credentialId,
      serviceIdentity: approval.serviceIdentity,
      subject: {
        jobId: approval.jobId,
        instanceId: approval.instanceId,
        nodeId: approval.nodeId
      },
      audience: approval.audience,
      permissions: [...approval.permissions],
      imageDigest: identity.imageDigest,
      lease: { leaseId: approval.leaseId, version: approval.leaseVersion },
      measurementId: approval.measurementId,
      approvalId: approval.approvalId,
      issuedAt: this.#clock.now()
    };

    return this.#allow(context, {
      approvalId: approval.approvalId,
      credentialId,
      credential,
      basis: this.#basis(measurement, lease, identity, approval, revocation),
      evidence: { measurement, lease, identity, revocation: null, approval }
    });
  }

  // ---------------------------------------------------------------- 审计访问

  getDecisionLog() {
    return this.#decisions.list();
  }

  getDecision(decisionId) {
    return this.#decisions.findByDecisionId(decisionId);
  }

  getDecisionsForRequest(requestId) {
    return this.#decisions.findByRequestId(requestId);
  }

  // ---------------------------------------------------------------- 内部工具

  #requireApprovalRequest(req) {
    if (!req || typeof req !== 'object') throw new TypeError('请求必须是对象');
    for (const field of ['requestId', 'jobId', 'instanceId', 'nodeId', 'measurementId',
      'leaseId', 'serviceIdentity', 'audience']) {
      if (typeof req[field] !== 'string' || req[field].length === 0) {
        throw new TypeError(`请求字段 ${field} 必须是非空字符串`);
      }
    }
    if (!Array.isArray(req.permissions)) {
      throw new TypeError('请求字段 permissions 必须是数组');
    }
  }

  #leaseWindowProblem(lease) {
    const now = this.#clock.wallMs();
    if (lease.validFrom && Date.parse(lease.validFrom) > now) return 'LEASE_NOT_YET_VALID';
    if (lease.validUntil && Date.parse(lease.validUntil) < now) return 'LEASE_EXPIRED';
    return null;
  }

  #revocationReason(scope) {
    if (scope === 'IDENTITY') return 'IDENTITY_REVOKED';
    if (scope === 'AUDIENCE') return 'AUDIENCE_REVOKED';
    return 'PERMISSION_REVOKED';
  }

  #basis(measurement, lease, identity, approval, revocation = null) {
    return {
      measurement: {
        measurementId: measurement.measurementId,
        jobId: measurement.jobId,
        instanceId: measurement.instanceId,
        nodeId: measurement.nodeId,
        imageDigest: measurement.imageDigest,
        verified: measurement.verified
      },
      lease: {
        leaseId: lease.leaseId,
        version: lease.version,
        status: lease.status,
        validFrom: lease.validFrom,
        validUntil: lease.validUntil
      },
      identity: {
        serviceIdentity: identity.serviceIdentity,
        imageDigest: identity.imageDigest,
        allowedAudiences: [...identity.allowedAudiences]
      },
      bindings: { ...approval.bindings },
      revocationCheckedAt: this.#clock.now(),
      activeRevocation: revocation
    };
  }

  #allow(context, extra) {
    const decisionId = `dec-${this.#clock.next()}`;
    const entry = {
      decisionId,
      verdict: 'ALLOW',
      stage: context.stage,
      requestId: context.requestId,
      reasons: [],
      evaluatedAt: this.#clock.now(),
      ...extra
    };
    return this.#decisions.append(entry);
  }

  #deny(context, reasonCode, evidence) {
    const decisionId = `dec-${this.#clock.next()}`;
    const entry = {
      decisionId,
      verdict: 'DENY',
      stage: context.stage,
      requestId: context.requestId,
      reasons: [reason(reasonCode)],
      evaluatedAt: this.#clock.now(),
      evidence: evidence ?? null
    };
    return this.#decisions.append(entry);
  }
}
