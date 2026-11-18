import { randomUUID } from 'node:crypto';

/**
 * 网络准入策略输出。
 * - normal:               正常放行
 * - quarantine:           隔离（冻结 / 会话失效）
 * - pending-verification: 待核验（报告缺失、超期或缺项、未获接入点确认）
 */
export const POLICY = Object.freeze({
  NORMAL: 'normal',
  QUARANTINE: 'quarantine',
  PENDING_VERIFICATION: 'pending-verification',
});

const DEFAULT_REQUIRED_ITEMS = Object.freeze([
  'osPatch',
  'antivirus',
  'diskEncryption',
]);

const DEFAULT_RECOVERY_SERVICES = Object.freeze([
  'remediation-portal',
  'patch-server',
]);

const DEFAULT_REPORT_TTL_MS = 5 * 60 * 1000;
const DEFAULT_MAX_CLOCK_SKEW_MS = 30 * 1000;

/**
 * 网络准入控制器。
 *
 * 不变量：
 * 1. 合规报告必须绑定当前接入会话的挑战值与测量时间，超期或缺项一律拒绝，
 *    设备降级为“待核验”，但仍可访问指定的恢复服务。
 * 2. 接入点确认只对“设备当前会话”生效；换接入点后旧会话的迟到确认
 *    不能提升新会话权限。
 * 3. 设备被紧急冻结时，其全部会话（新旧接入点）都收敛到隔离策略；
 *    解冻后所有旧会话作废，必须按全新会话重新办理。
 */
export class NetworkAccessController {
  #sessions = new Map(); // sessionId -> session
  #devices = new Map(); // deviceId -> { currentSessionId, frozen }
  #reportTtlMs;
  #maxClockSkewMs;
  #requiredItems;
  #recoveryServices;
  #now;

  constructor({
    reportTtlMs = DEFAULT_REPORT_TTL_MS,
    maxClockSkewMs = DEFAULT_MAX_CLOCK_SKEW_MS,
    requiredItems = DEFAULT_REQUIRED_ITEMS,
    recoveryServices = DEFAULT_RECOVERY_SERVICES,
    now = () => Date.now(),
  } = {}) {
    if (!(reportTtlMs > 0)) throw new RangeError('reportTtlMs must be > 0');
    if (!(maxClockSkewMs >= 0)) throw new RangeError('maxClockSkewMs must be >= 0');
    if (!Array.isArray(requiredItems) || requiredItems.length === 0) {
      throw new TypeError('requiredItems must be a non-empty array');
    }
    if (!Array.isArray(recoveryServices)) {
      throw new TypeError('recoveryServices must be an array');
    }
    if (typeof now !== 'function') throw new TypeError('now must be a function');
    this.#reportTtlMs = reportTtlMs;
    this.#maxClockSkewMs = maxClockSkewMs;
    this.#requiredItems = [...requiredItems];
    this.#recoveryServices = [...recoveryServices];
    this.#now = now;
  }

  #device(deviceId) {
    let entry = this.#devices.get(deviceId);
    if (!entry) {
      entry = { currentSessionId: null, frozen: false };
      this.#devices.set(deviceId, entry);
    }
    return entry;
  }

  /**
   * 为设备在当前接入点开启新会话。新会话取代旧会话成为“当前会话”，
   * 旧会话保留但不再接受确认（迟到确认不得提升新会话）。
   */
  openSession({ deviceId, accessPointId } = {}) {
    if (typeof deviceId !== 'string' || deviceId.length === 0) {
      throw new TypeError('deviceId must be a non-empty string');
    }
    if (typeof accessPointId !== 'string' || accessPointId.length === 0) {
      throw new TypeError('accessPointId must be a non-empty string');
    }
    const session = {
      sessionId: randomUUID(),
      deviceId,
      accessPointId,
      challenge: randomUUID(),
      issuedAt: this.#now(),
      state: 'active',
      report: null, // { measuredAt, receivedAt, items }
      confirmed: false,
    };
    this.#sessions.set(session.sessionId, session);
    this.#device(deviceId).currentSessionId = session.sessionId;
    return Object.freeze({
      sessionId: session.sessionId,
      deviceId: session.deviceId,
      accessPointId: session.accessPointId,
      challenge: session.challenge,
      issuedAt: session.issuedAt,
    });
  }

  /**
   * 提交合规报告。报告必须绑定该会话当前的挑战值，测量时间不得超期
   * 或超出时钟容差来自未来，且必选项齐全。
   * 返回 { accepted, reason?, missing? }。
   */
  submitReport({ deviceId, sessionId, challenge, measuredAt, items } = {}) {
    const session = this.#sessions.get(sessionId);
    if (!session || session.state !== 'active') {
      return { accepted: false, reason: 'unknown-session' };
    }
    if (session.deviceId !== deviceId) {
      return { accepted: false, reason: 'device-mismatch' };
    }
    if (this.#device(deviceId).frozen) {
      return { accepted: false, reason: 'device-frozen' };
    }
    if (challenge !== session.challenge) {
      return { accepted: false, reason: 'challenge-mismatch' };
    }
    if (typeof measuredAt !== 'number' || !Number.isFinite(measuredAt)) {
      return { accepted: false, reason: 'invalid-measurement-time' };
    }
    const now = this.#now();
    if (measuredAt > now + this.#maxClockSkewMs) {
      return { accepted: false, reason: 'future-measurement' };
    }
    if (now - measuredAt > this.#reportTtlMs) {
      return { accepted: false, reason: 'expired' };
    }
    const given = items ?? {};
    const missing = this.#requiredItems.filter(
      (key) => given[key] === undefined || given[key] === null,
    );
    if (missing.length > 0) {
      return { accepted: false, reason: 'incomplete-items', missing };
    }
    session.report = {
      measuredAt,
      receivedAt: now,
      items: { ...given },
    };
    return { accepted: true };
  }

  /**
   * 接入点确认。只对设备的“当前会话”生效：换接入点后，旧会话的迟到
   * 确认会被拒绝，无法提升新会话权限。
   */
  confirmAccessPoint({ sessionId, challenge, accessPointId } = {}) {
    const session = this.#sessions.get(sessionId);
    if (!session || session.state !== 'active') {
      return { accepted: false, reason: 'unknown-session' };
    }
    const device = this.#device(session.deviceId);
    if (device.frozen) {
      return { accepted: false, reason: 'device-frozen' };
    }
    if (device.currentSessionId !== sessionId) {
      return { accepted: false, reason: 'superseded-session' };
    }
    if (challenge !== session.challenge) {
      return { accepted: false, reason: 'challenge-mismatch' };
    }
    if (accessPointId !== session.accessPointId) {
      return { accepted: false, reason: 'access-point-mismatch' };
    }
    session.confirmed = true;
    return { accepted: true };
  }

  /**
   * 评估设备某会话应下发的网络策略。
   * 返回 { deviceId, sessionId, policy, allowedServices, reason }。
   * - normal:              allowedServices 为 ['*']（不限制）
   * - pending-verification: allowedServices 为恢复服务列表
   * - quarantine:          allowedServices 为 []
   */
  evaluate({ deviceId, sessionId } = {}) {
    const device = this.#devices.get(deviceId);
    if (device?.frozen) {
      return this.#result(deviceId, sessionId, POLICY.QUARANTINE, [], 'device-frozen');
    }
    const session = this.#sessions.get(sessionId);
    if (!session) {
      return this.#result(deviceId, sessionId, POLICY.QUARANTINE, [], 'session-unknown');
    }
    if (session.state !== 'active') {
      return this.#result(deviceId, sessionId, POLICY.QUARANTINE, [], 'session-closed');
    }
    if (session.deviceId !== deviceId) {
      return this.#result(deviceId, sessionId, POLICY.QUARANTINE, [], 'device-mismatch');
    }
    if (!session.report) {
      return this.#pending(deviceId, sessionId, 'no-valid-report');
    }
    if (this.#now() - session.report.measuredAt > this.#reportTtlMs) {
      // 测量时间已超期：降级为待核验，但保留恢复服务通道。
      return this.#pending(deviceId, sessionId, 'report-expired');
    }
    if (!session.confirmed) {
      return this.#pending(deviceId, sessionId, 'awaiting-confirmation');
    }
    return this.#result(deviceId, sessionId, POLICY.NORMAL, ['*'], 'ok');
  }

  /** 紧急冻结：该设备所有会话（新旧接入点）收敛到隔离策略。 */
  freezeDevice(deviceId) {
    this.#device(deviceId).frozen = true;
  }

  /**
   * 解除冻结：作废旧设备全部会话与当前会话指针，恢复后必须
   * 按全新会话重新办理（旧报告的挑战值也随之失效）。
   */
  unfreezeDevice(deviceId) {
    const device = this.#device(deviceId);
    device.frozen = false;
    device.currentSessionId = null;
    for (const session of this.#sessions.values()) {
      if (session.deviceId === deviceId && session.state === 'active') {
        session.state = 'closed';
      }
    }
  }

  #pending(deviceId, sessionId, reason) {
    return this.#result(
      deviceId,
      sessionId,
      POLICY.PENDING_VERIFICATION,
      [...this.#recoveryServices],
      reason,
    );
  }

  #result(deviceId, sessionId, policy, allowedServices, reason) {
    return Object.freeze({ deviceId, sessionId, policy, allowedServices, reason });
  }
}
