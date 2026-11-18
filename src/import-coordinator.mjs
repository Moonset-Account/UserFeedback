/**
 * 导入协调模块（托管系统密钥材料登记协调）。
 *
 * 输入三类事实：
 *   1. 业务签发的用途申请   —— submitApplication / submitApplicationBatch
 *   2. 受信设备确认的材料指纹 —— confirmMaterial
 *   3. 待启用的设备登记     —— registerDevice
 * 设备回执通过 submitReceipt 进入。
 *
 * 不变式：
 *   - 同一材料指纹的用途全局一致；不一致的申请/登记被拒绝。
 *   - 同时到达的同材料竞争申请按确定性规则裁决（issuedAt 升序，其次 applicationId 字典序）。
 *   - 回执仅证明导入成功而未证明用途绑定时，登记停留在 pending_verification（待核验）。
 *   - 重试（相同 registrationId 且参数一致）延续原登记；旧回执不能启用新设备登记。
 */

export const RegistrationState = Object.freeze({
  PENDING: 'pending',
  PENDING_VERIFICATION: 'pending_verification',
  ACTIVE: 'active',
});

export const ApplicationStatus = Object.freeze({
  ACCEPTED: 'accepted',
  REJECTED: 'rejected',
});

export const RejectReason = Object.freeze({
  PURPOSE_MISMATCH: 'purpose-mismatch',
  PURPOSE_COMPETITION_LOST: 'purpose-competition-lost',
  UNKNOWN_MATERIAL: 'unknown-material',
  NO_ACCEPTED_APPLICATION: 'no-accepted-application',
  RETRY_MISMATCH: 'retry-mismatch',
  UNKNOWN_REGISTRATION: 'unknown-registration',
  RECEIPT_MISMATCH: 'receipt-mismatch',
  STALE_RECEIPT: 'stale-receipt',
});

function requireNonEmptyString(value, field) {
  if (typeof value !== 'string' || value.length === 0) {
    throw new TypeError(`field "${field}" must be a non-empty string`);
  }
}

function requireTimestamp(value, field) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new TypeError(`field "${field}" must be a finite number`);
  }
}

function normalizeApplication(raw) {
  if (raw === null || typeof raw !== 'object') {
    throw new TypeError('application must be an object');
  }
  requireNonEmptyString(raw.applicationId, 'applicationId');
  requireNonEmptyString(raw.businessId, 'businessId');
  requireNonEmptyString(raw.fingerprint, 'fingerprint');
  requireNonEmptyString(raw.purpose, 'purpose');
  requireTimestamp(raw.issuedAt, 'issuedAt');
  return {
    applicationId: raw.applicationId,
    businessId: raw.businessId,
    fingerprint: raw.fingerprint,
    purpose: raw.purpose,
    issuedAt: raw.issuedAt,
  };
}

function normalizeRegistration(raw) {
  if (raw === null || typeof raw !== 'object') {
    throw new TypeError('registration must be an object');
  }
  requireNonEmptyString(raw.registrationId, 'registrationId');
  requireNonEmptyString(raw.deviceId, 'deviceId');
  requireNonEmptyString(raw.fingerprint, 'fingerprint');
  requireNonEmptyString(raw.purpose, 'purpose');
  requireTimestamp(raw.createdAt, 'createdAt');
  if (raw.applicationId !== undefined) {
    requireNonEmptyString(raw.applicationId, 'applicationId');
  }
  return {
    registrationId: raw.registrationId,
    deviceId: raw.deviceId,
    fingerprint: raw.fingerprint,
    purpose: raw.purpose,
    createdAt: raw.createdAt,
    applicationId: raw.applicationId,
  };
}

function normalizeReceipt(raw) {
  if (raw === null || typeof raw !== 'object') {
    throw new TypeError('receipt must be an object');
  }
  requireNonEmptyString(raw.receiptId, 'receiptId');
  requireNonEmptyString(raw.registrationId, 'registrationId');
  requireNonEmptyString(raw.deviceId, 'deviceId');
  requireNonEmptyString(raw.fingerprint, 'fingerprint');
  requireTimestamp(raw.issuedAt, 'issuedAt');
  return {
    receiptId: raw.receiptId,
    registrationId: raw.registrationId,
    deviceId: raw.deviceId,
    fingerprint: raw.fingerprint,
    importProven: raw.importProven === true,
    purposeBindingProven: raw.purposeBindingProven === true,
    issuedAt: raw.issuedAt,
  };
}

/** 竞争裁决：issuedAt 小者优先；并列时 applicationId 字典序小者优先。 */
function compareApplications(a, b) {
  if (a.issuedAt !== b.issuedAt) return a.issuedAt - b.issuedAt;
  return a.applicationId < b.applicationId ? -1 : a.applicationId > b.applicationId ? 1 : 0;
}

function snapshotRegistration(reg) {
  return {
    registrationId: reg.registrationId,
    deviceId: reg.deviceId,
    fingerprint: reg.fingerprint,
    purpose: reg.purpose,
    state: reg.state,
    createdAt: reg.createdAt,
    enabledAt: reg.enabledAt,
    lastReceiptAt: reg.lastReceiptAt,
    receipts: [...reg.receipts],
  };
}

export class ImportCoordinator {
  #materials = new Map(); // fingerprint -> { fingerprint, confirmedBy, confirmedAt }
  #lockedPurpose = new Map(); // fingerprint -> purpose（由被接受的申请锁定）
  #applications = new Map(); // applicationId -> { application, status, reason }
  #registrations = new Map(); // registrationId -> registration record
  #receiptIds = new Set();

  /**
   * 受信设备确认材料指纹。幂等：重复确认返回已有记录。
   */
  confirmMaterial({ fingerprint, confirmedBy = 'trusted-device', confirmedAt = 0 } = {}) {
    requireNonEmptyString(fingerprint, 'fingerprint');
    const existing = this.#materials.get(fingerprint);
    if (existing) {
      return { ok: true, duplicate: true, material: { ...existing } };
    }
    const material = { fingerprint, confirmedBy, confirmedAt };
    this.#materials.set(fingerprint, material);
    return { ok: true, duplicate: false, material: { ...material } };
  }

  /**
   * 单笔用途申请。等价于 submitApplicationBatch([application]) 的单元素形式：
   * 与已锁定用途冲突时拒绝（purpose-mismatch）。
   */
  submitApplication(application) {
    return this.submitApplicationBatch([application])[0];
  }

  /**
   * 一批同时到达的用途申请。同一材料指纹下用途互相冲突的申请按确定性规则
   * 裁决：胜出用途锁定材料，其余申请以 purpose-competition-lost 拒绝。
   * 结果与批次内提交顺序无关。
   */
  submitApplicationBatch(applications) {
    if (!Array.isArray(applications) || applications.length === 0) {
      throw new TypeError('applications must be a non-empty array');
    }
    const normalized = applications.map(normalizeApplication);
    const byFingerprint = new Map();
    for (const app of normalized) {
      if (!byFingerprint.has(app.fingerprint)) byFingerprint.set(app.fingerprint, []);
      byFingerprint.get(app.fingerprint).push(app);
    }

    const decisionsById = new Map();
    for (const [fingerprint, group] of [...byFingerprint.entries()].sort()) {
      const fresh = [];
      for (const app of group) {
        const prior = this.#applications.get(app.applicationId);
        if (prior) {
          decisionsById.set(app.applicationId, { ...prior, duplicate: true });
        } else {
          fresh.push(app);
        }
      }
      if (fresh.length === 0) continue;

      const locked = this.#lockedPurpose.get(fingerprint);
      let winningPurpose = locked;
      if (winningPurpose === undefined) {
        const winner = [...fresh].sort(compareApplications)[0];
        winningPurpose = winner.purpose;
        this.#lockedPurpose.set(fingerprint, winningPurpose);
      }

      for (const app of fresh) {
        let decision;
        if (app.purpose === winningPurpose) {
          decision = { ok: true, applicationId: app.applicationId, status: ApplicationStatus.ACCEPTED };
        } else {
          decision = {
            ok: false,
            applicationId: app.applicationId,
            status: ApplicationStatus.REJECTED,
            reason: locked === undefined
              ? RejectReason.PURPOSE_COMPETITION_LOST
              : RejectReason.PURPOSE_MISMATCH,
          };
        }
        this.#applications.set(app.applicationId, { application: app, ...decision });
        decisionsById.set(app.applicationId, { application: app, ...decision, duplicate: false });
      }
    }

    return normalized.map((app) => decisionsById.get(app.applicationId));
  }

  /**
   * 待启用的设备登记。
   *  - 重试（同 registrationId 且设备/指纹/用途一致）延续原登记，不新建、不重置状态；
   *  - 同 registrationId 但参数不一致视为非法重试，拒绝；
   *  - 材料指纹必须已由受信设备确认，且用途与已锁定用途一致。
   */
  registerDevice(registration) {
    const reg = normalizeRegistration(registration);

    const existing = this.#registrations.get(reg.registrationId);
    if (existing) {
      const sameShape =
        existing.deviceId === reg.deviceId &&
        existing.fingerprint === reg.fingerprint &&
        existing.purpose === reg.purpose;
      if (sameShape) {
        return { ok: true, continued: true, registration: snapshotRegistration(existing) };
      }
      return {
        ok: false,
        continued: false,
        reason: RejectReason.RETRY_MISMATCH,
        registration: snapshotRegistration(existing),
      };
    }

    if (!this.#materials.has(reg.fingerprint)) {
      return { ok: false, reason: RejectReason.UNKNOWN_MATERIAL };
    }
    const locked = this.#lockedPurpose.get(reg.fingerprint);
    if (locked === undefined) {
      return { ok: false, reason: RejectReason.NO_ACCEPTED_APPLICATION };
    }
    if (locked !== reg.purpose) {
      return { ok: false, reason: RejectReason.PURPOSE_MISMATCH };
    }
    if (reg.applicationId !== undefined) {
      const appDecision = this.#applications.get(reg.applicationId);
      if (
        !appDecision ||
        appDecision.status !== ApplicationStatus.ACCEPTED ||
        appDecision.application.fingerprint !== reg.fingerprint ||
        appDecision.application.purpose !== reg.purpose
      ) {
        return { ok: false, reason: RejectReason.NO_ACCEPTED_APPLICATION };
      }
    }

    const record = {
      registrationId: reg.registrationId,
      deviceId: reg.deviceId,
      fingerprint: reg.fingerprint,
      purpose: reg.purpose,
      state: RegistrationState.PENDING,
      createdAt: reg.createdAt,
      enabledAt: null,
      lastReceiptAt: null,
      receipts: [],
    };
    this.#registrations.set(record.registrationId, record);
    return { ok: true, continued: false, registration: snapshotRegistration(record) };
  }

  /**
   * 设备回执。
   *  - 回执必须属于既存登记，且 deviceId / fingerprint 与登记一致；
   *  - 早于登记创建时间（或早于已应用的最新回执）的旧回执被拒绝，不能启用登记；
   *  - 仅证明导入成功 → pending_verification（待核验）；
   *  - 同时证明用途绑定 → active（启用）；已启用的登记不会被弱证明降级。
   */
  submitReceipt(receipt) {
    const rc = normalizeReceipt(receipt);

    if (this.#receiptIds.has(rc.receiptId)) {
      const reg = this.#registrations.get(rc.registrationId);
      return { ok: true, duplicate: true, state: reg ? reg.state : undefined };
    }
    const reg = this.#registrations.get(rc.registrationId);
    if (!reg) {
      return { ok: false, reason: RejectReason.UNKNOWN_REGISTRATION };
    }
    if (rc.deviceId !== reg.deviceId || rc.fingerprint !== reg.fingerprint) {
      return { ok: false, reason: RejectReason.RECEIPT_MISMATCH, state: reg.state };
    }
    if (rc.issuedAt < reg.createdAt) {
      return { ok: false, reason: RejectReason.STALE_RECEIPT, state: reg.state };
    }
    if (reg.lastReceiptAt !== null && rc.issuedAt < reg.lastReceiptAt) {
      return { ok: false, reason: RejectReason.STALE_RECEIPT, state: reg.state };
    }

    this.#receiptIds.add(rc.receiptId);
    reg.lastReceiptAt = rc.issuedAt;
    reg.receipts.push(rc.receiptId);

    if (reg.state !== RegistrationState.ACTIVE) {
      if (rc.importProven && rc.purposeBindingProven) {
        reg.state = RegistrationState.ACTIVE;
        reg.enabledAt = rc.issuedAt;
      } else if (rc.importProven) {
        reg.state = RegistrationState.PENDING_VERIFICATION;
      }
    }
    return { ok: true, duplicate: false, state: reg.state };
  }

  getRegistration(registrationId) {
    const reg = this.#registrations.get(registrationId);
    return reg ? snapshotRegistration(reg) : undefined;
  }

  getApplication(applicationId) {
    const decision = this.#applications.get(applicationId);
    return decision ? { ...decision, application: { ...decision.application } } : undefined;
  }

  getMaterial(fingerprint) {
    const material = this.#materials.get(fingerprint);
    if (!material) return undefined;
    return { ...material, lockedPurpose: this.#lockedPurpose.get(fingerprint) };
  }
}

export function createImportCoordinator() {
  return new ImportCoordinator();
}
