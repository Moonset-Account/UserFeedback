import { PaperControllerError, ErrorCode } from './errors.js';

/**
 * 纸张生命周期状态。
 *
 * REGISTERED   —— 新纸登记，正常流转中
 * QUARANTINED  —— 卡纸隔离：旧纸未经回收确认，永远不可开格
 * RECYCLED     —— 回收确认完成，审计留痕
 * DELIVERED    —— 所有证据核齐，已向对应取件格开格一次
 */
export const SheetStatus = Object.freeze({
  REGISTERED: 'REGISTERED',
  QUARANTINED: 'QUARANTINED',
  RECYCLED: 'RECYCLED',
  DELIVERED: 'DELIVERED',
});

/** 成像面。 */
export const Side = Object.freeze({
  FRONT: 'FRONT',
  BACK: 'BACK',
});

/** 背面策略：单面任务必须显式声明空白背面，双面任务要求实印背面。 */
export const BackPolicy = Object.freeze({
  PRINTED: 'PRINTED',
  BLANK: 'BLANK',
});

const VALID_EVENT_TYPES = new Set([
  'PAPER_REGISTERED',
  'SIDE_IMAGED',
  'PAPER_OUTPUT',
  'PICKUP_AUTHORIZED',
  'PAPER_JAMMED',
  'PAPER_RECYCLED',
]);

function requireString(value, field, eventId) {
  if (typeof value !== 'string' || value.length === 0) {
    throw new PaperControllerError(
      ErrorCode.MALFORMED_EVENT,
      `事件缺少非空字符串字段 ${field}`,
      { eventId, field },
    );
  }
}

function ok(eventId, accepted, sheet, extra = {}) {
  return {
    ok: true,
    eventId,
    accepted,
    paperId: sheet.paperId,
    status: sheet.status,
    ...extra,
  };
}

function reject(code, message, context) {
  return new PaperControllerError(code, message, context);
}

/**
 * 保密出纸控制器。
 *
 * 核心不变量（每处理一个事件后都必须成立）：
 *  1. 所有证据（FRONT/BACK 完成记录、出纸传感器记录、取件授权）都以 paperId
 *     绑定到同一张实体纸；任何指向不存在纸张、纸张归属不符的事件一律拒绝。
 *  2. 只有同一纸号上证据齐备且状态仍为 REGISTERED 时才允许开格；
 *     卡纸隔离（QUARANTINED）的旧纸即使补齐剩余事件也不可开格。
 *  3. 每张纸最多开格一次：delivered 幂等，事件重放 / 重复上报不会二次开格。
 *  4. eventId 去重：重复事件返回首次处理结果，不产生任何副作用。
 *  5. 乱序安全：事件可任意顺序到达，只在证据齐备时尝试开格；缺证据就等待。
 */
export class PaperOutputController {
  /**
   * @param {object} [options]
   * @param {import('./ports.js').OutputSensor} [options.sensor] 初始出纸传感器（可后续替换）
   * @param {import('./ports.js').CompartmentGate} [options.gate] 初始开格回调（可后续替换）
   * @param {() => number} [options.clock] 时间戳来源（便于测试）
   */
  constructor(options = {}) {
    /** @type {Map<string, object>} paperId -> 纸张聚合 */
    this._sheets = new Map();
    /** @type {Map<string, {result: object}>} eventId -> 首次处理结果（重放去重） */
    this._events = new Map();
    /** @type {Map<string, import('./ports.js').OutputSensor>} sensorId -> 传感器 */
    this._sensors = new Map();
    /** @type {import('./ports.js').CompartmentGate|null} */
    this._gate = options.gate ?? null;
    this._clock = options.clock ?? (() => Date.now());

    if (options.sensor) {
      this.registerSensor(options.sensor);
    }
  }

  // ---------------------------------------------------------------- 端口管理

  /**
   * 登记 / 替换可替换的出纸传感器。相同 sensorId 视为替换硬件：
   * 已记录在纸上的物理出纸事实保留（那是已发生的事实），新事件需来自在册传感器。
   */
  registerSensor(sensor) {
    if (!sensor || typeof sensor.sensorId !== 'string' || sensor.sensorId.length === 0) {
      throw reject(ErrorCode.MALFORMED_EVENT, '传感器必须提供非空 sensorId', {});
    }
    this._sensors.set(sensor.sensorId, sensor);
    return sensor.sensorId;
  }

  /** 移除传感器（硬件拔出 / 维修）。在册之外的传感器上报将被拒绝。 */
  removeSensor(sensorId) {
    return this._sensors.delete(sensorId);
  }

  getSensor(sensorId) {
    return this._sensors.get(sensorId) ?? null;
  }

  /** 替换开格回调（闸门维修 / 换格）。不影响已 DELIVERED 纸张的幂等结论。 */
  setGate(gate) {
    this._gate = gate ?? null;
  }

  getGate() {
    return this._gate;
  }

  // -------------------------------------------------------------------- 查询

  getSheet(paperId) {
    const sheet = this._sheets.get(paperId);
    return sheet ? this._snapshot(sheet) : null;
  }

  /** 深快照，测试与审计使用，外部无法篡改内部状态。 */
  _snapshot(sheet) {
    return structuredClone({
      ...sheet,
      imagedSides: [...sheet.imagedSides],
      audit: [...sheet.audit],
    });
  }

  /** 全部纸张的只读快照。 */
  listSheets() {
    return [...this._sheets.values()].map((s) => this._snapshot(s));
  }

  /** 已处理事件 id（重放去重表）。 */
  getProcessedEventIds() {
    return [...this._events.keys()];
  }

  // -------------------------------------------------------------- 事件入口

  /**
   * 处理单个事件。重复 eventId 直接回放首次结果（无副作用）；
   * 非法事件抛 PaperControllerError，且不写入去重表（同一事件修正后可重投）。
   *
   * @param {object} event
   * @returns {object} 处理结果
   */
  handle(event) {
    if (!event || typeof event !== 'object') {
      throw reject(ErrorCode.MALFORMED_EVENT, '事件必须是对象', {});
    }
    requireString(event.eventId, 'eventId', event.eventId);
    requireString(event.type, 'type', event.eventId);

    const seen = this._events.get(event.eventId);
    if (seen) {
      // 重放：原样返回首次结论，绝不重复开格 / 重复改状态。
      return structuredClone(seen.result);
    }

    if (!VALID_EVENT_TYPES.has(event.type)) {
      throw reject(
        ErrorCode.UNKNOWN_EVENT_TYPE,
        `未知事件类型 ${event.type}`,
        { eventId: event.eventId },
      );
    }

    const result = this._dispatch(event);
    this._events.set(event.eventId, { result: structuredClone(result) });
    return structuredClone(result);
  }

  /** 便捷批量入口：逐条重放，返回每条结果（单条失败不影响其它事件）。 */
  handleMany(events) {
    const results = [];
    for (const event of events) {
      results.push(this.handle(event));
    }
    return results;
  }

  _dispatch(event) {
    switch (event.type) {
      case 'PAPER_REGISTERED':
        return this._onRegistered(event);
      case 'SIDE_IMAGED':
        return this._onSideImaged(event);
      case 'PAPER_OUTPUT':
        return this._onPaperOutput(event);
      case 'PICKUP_AUTHORIZED':
        return this._onPickupAuthorized(event);
      case 'PAPER_JAMMED':
        return this._onJammed(event);
      case 'PAPER_RECYCLED':
        return this._onRecycled(event);
      default:
        // 理论不可达：类型集合已在 handle 校验。
        throw reject(ErrorCode.UNKNOWN_EVENT_TYPE, `未分发的事件类型 ${event.type}`, {
          eventId: event.eventId,
        });
    }
  }

  // ------------------------------------------------------------- 事件处理

  _onRegistered(event) {
    const { eventId, paperId, ownerId, jobId, compartmentId, backPolicy } = event;
    requireString(paperId, 'paperId', eventId);
    requireString(ownerId, 'ownerId', eventId);
    requireString(jobId, 'jobId', eventId);
    requireString(compartmentId, 'compartmentId', eventId);

    if (backPolicy !== BackPolicy.PRINTED && backPolicy !== BackPolicy.BLANK) {
      // 单面任务必须显式声明 BLANK 背面，双面任务声明 PRINTED；缺失即拒绝登记。
      throw reject(
        ErrorCode.INVALID_SIDE_POLICY,
        `backPolicy 必须是 ${BackPolicy.PRINTED} 或 ${BackPolicy.BLANK}，单面任务须显式声明空白背面`,
        { eventId, paperId, backPolicy },
      );
    }

    if (this._sheets.has(paperId)) {
      throw reject(
        ErrorCode.PAPER_ALREADY_REGISTERED,
        `纸号 ${paperId} 已登记，实体纸号不可复用`,
        { eventId, paperId },
      );
    }

    const sheet = {
      paperId,
      ownerId,
      jobId,
      compartmentId,
      backPolicy,
      status: SheetStatus.REGISTERED,
      imagedSides: [],
      output: null, // { sensorId, at }
      authorization: null, // { ownerId, at }
      jam: null,
      recycledAt: null,
      deliveredAt: null,
      gateCalls: [],
      audit: [],
    };
    sheet.audit.push({ at: this._clock(), eventId, type: event.type });
    this._sheets.set(paperId, sheet);
    return ok(eventId, true, sheet);
  }

  _onSideImaged(event) {
    const { eventId, paperId, ownerId, jobId, side } = event;
    const sheet = this._requireActiveSheet(event);
    requireString(side, 'side', eventId);

    if (side !== Side.FRONT && side !== Side.BACK) {
      throw reject(ErrorCode.SIDE_MISMATCH, `未知成像面 ${side}`, { eventId, paperId, side });
    }

    // 归属核对：半面回执必须与纸张登记的同一人、同一任务一致，
    // 从源头阻止把 A 任务 / A 纸的回执挂到 B 纸上。
    if (sheet.ownerId !== ownerId || sheet.jobId !== jobId) {
      throw reject(
        ErrorCode.WRONG_PAPER_OWNER,
        '成像回执的归属人或任务与目标纸张不一致，禁止跨纸补齐半面',
        { eventId, paperId, expectedOwner: sheet.ownerId, gotOwner: ownerId, expectedJob: sheet.jobId, gotJob: jobId },
      );
    }

    // 单面任务声明了空白背面：不允许再出现背面实印记录。
    if (side === Side.BACK && sheet.backPolicy === BackPolicy.BLANK) {
      throw reject(
        ErrorCode.SIDE_MISMATCH,
        '该纸为单面任务（背面声明为空白），不得登记背面实印',
        { eventId, paperId },
      );
    }

    return this._withGateRollback(sheet, (target) => {
      const already = target.imagedSides.includes(side);
      if (!already) {
        target.imagedSides.push(side);
        target.audit.push({ at: this._clock(), eventId, type: event.type, side });
      }
      // 同面重复上报：幂等接受，不重复记录。
      const release = this._tryRelease(target, eventId);
      return ok(eventId, !already, target, { side, release });
    });
  }

  _onPaperOutput(event) {
    const { eventId, paperId, sensorId } = event;
    const sheet = this._requireActiveSheet(event);
    requireString(sensorId, 'sensorId', eventId);

    // 传感器必须在册：可替换硬件，但未登记 / 已移除的传感器上报不可信。
    if (!this._sensors.has(sensorId)) {
      throw reject(
        ErrorCode.UNKNOWN_SENSOR,
        `传感器 ${sensorId} 未在册，出纸上报被拒绝`,
        { eventId, paperId, sensorId },
      );
    }

    return this._withGateRollback(sheet, (target) => {
      const already = target.output !== null;
      if (!already) {
        target.output = { sensorId, at: this._clock() };
        target.audit.push({ at: this._clock(), eventId, type: event.type, sensorId });
      }
      // 重复出纸上报幂等：保留首次传感器事实。
      const release = this._tryRelease(target, eventId);
      return ok(eventId, !already, target, { release });
    });
  }

  _onPickupAuthorized(event) {
    const { eventId, paperId, ownerId } = event;
    const sheet = this._requireActiveSheet(event);
    requireString(ownerId, 'ownerId', eventId);

    // 取件授权必须与纸张归属同一人，否则拒绝。
    if (sheet.ownerId !== ownerId) {
      throw reject(
        ErrorCode.WRONG_OWNER,
        '取件授权人与纸张归属人不一致，禁止打开他人取件格',
        { eventId, paperId, expectedOwner: sheet.ownerId, gotOwner: ownerId },
      );
    }

    return this._withGateRollback(sheet, (target) => {
      const already = target.authorization !== null;
      if (!already) {
        target.authorization = { ownerId, at: this._clock() };
        target.audit.push({ at: this._clock(), eventId, type: event.type, ownerId });
      }
      const release = this._tryRelease(target, eventId);
      return ok(eventId, !already, target, { release });
    });
  }

  _onJammed(event) {
    const { eventId, paperId } = event;
    requireString(paperId, 'paperId', eventId);
    // 卡纸事件针对的纸号必须存在；不存在则拒绝，防止伪造隔离一张正常纸。
    const sheet = this._sheets.get(paperId);
    if (!sheet) {
      throw reject(ErrorCode.UNKNOWN_PAPER, `卡纸事件指向未知纸号 ${paperId}`, { eventId, paperId });
    }

    if (sheet.status === SheetStatus.DELIVERED) {
      throw reject(
        ErrorCode.PAPER_DELIVERED,
        `纸 ${paperId} 已交付，不可再标记卡纸`,
        { eventId, paperId },
      );
    }
    if (sheet.status === SheetStatus.RECYCLED) {
      throw reject(
        ErrorCode.PAPER_RECYCLED,
        `纸 ${paperId} 已回收，不可再标记卡纸`,
        { eventId, paperId },
      );
    }

    const already = sheet.status === SheetStatus.QUARANTINED;
    if (!already) {
      sheet.status = SheetStatus.QUARANTINED;
      sheet.jam = { at: this._clock(), eventId };
      sheet.audit.push({ at: this._clock(), eventId, type: event.type });
    }
    // 重复卡纸上报幂等。隔离态下任何后续事件都不能再触发开格。
    return ok(eventId, !already, sheet);
  }

  _onRecycled(event) {
    const { eventId, paperId } = event;
    requireString(paperId, 'paperId', eventId);
    const sheet = this._sheets.get(paperId);
    if (!sheet) {
      throw reject(ErrorCode.UNKNOWN_PAPER, `回收事件指向未知纸号 ${paperId}`, { eventId, paperId });
    }

    if (sheet.status === SheetStatus.DELIVERED) {
      throw reject(
        ErrorCode.PAPER_DELIVERED,
        `纸 ${paperId} 已交付，不存在回收流程`,
        { eventId, paperId },
      );
    }
    if (sheet.status === SheetStatus.RECYCLED) {
      // 重复回收确认幂等。
      return ok(eventId, false, sheet);
    }
    // 只有隔离纸需要回收确认；正常在途纸不能直接走回收，避免绕过隔离审计。
    if (sheet.status !== SheetStatus.QUARANTINED) {
      throw reject(
        ErrorCode.PAPER_QUARANTINED,
        `纸 ${paperId} 未处于隔离状态，不能执行回收确认`,
        { eventId, paperId, status: sheet.status },
      );
    }

    sheet.status = SheetStatus.RECYCLED;
    sheet.recycledAt = this._clock();
    sheet.audit.push({ at: this._clock(), eventId, type: event.type });
    return ok(eventId, true, sheet);
  }

  // ------------------------------------------------------------- 核心开格

  /**
   * 取在册纸张；卡纸隔离 / 回收 / 已交付的纸张拒绝继续接收证据事件。
   * 这保证旧纸的迟到半面回执不会“补齐”任何东西——它连入口都进不来，
   * 更不可能落到新纸号上（新纸号是另一聚合，paperId 不匹配同样拒绝）。
   */
  _requireActiveSheet(event) {
    const { eventId, paperId } = event;
    requireString(paperId, 'paperId', eventId);
    const sheet = this._sheets.get(paperId);
    if (!sheet) {
      throw reject(ErrorCode.UNKNOWN_PAPER, `事件指向未知纸号 ${paperId}`, { eventId, paperId });
    }
    switch (sheet.status) {
      case SheetStatus.QUARANTINED:
        throw reject(
          ErrorCode.PAPER_QUARANTINED,
          `纸 ${paperId} 已因卡纸隔离，等待回收确认，证据事件被拒绝`,
          { eventId, paperId },
        );
      case SheetStatus.RECYCLED:
        throw reject(
          ErrorCode.PAPER_RECYCLED,
          `纸 ${paperId} 已回收，不再接受事件`,
          { eventId, paperId },
        );
      case SheetStatus.DELIVERED:
        throw reject(
          ErrorCode.PAPER_DELIVERED,
          `纸 ${paperId} 已交付开格，不再接受事件`,
          { eventId, paperId },
        );
      default:
        return sheet;
    }
  }

  /**
   * 证据齐备性判定 —— 所有证据必须核到同一张纸（即本 sheet 聚合内）：
   *   - FRONT 完成；
   *   - BACK：双面任务要求实印记录，单面任务凭登记时显式声明的 BLANK 政策满足；
   *   - 在册传感器的实际出纸记录；
   *   - 同一 ownerId 的取件授权。
   * 全部满足且纸张仍为 REGISTERED 才开格，且只开一次。
   */
  _isEvidenceComplete(sheet) {
    const frontDone = sheet.imagedSides.includes(Side.FRONT);
    const backDone =
      sheet.backPolicy === BackPolicy.BLANK || sheet.imagedSides.includes(Side.BACK);
    return Boolean(
      frontDone &&
      backDone &&
      sheet.output &&
      sheet.authorization &&
      sheet.authorization.ownerId === sheet.ownerId,
    );
  }

  /**
   * 事件原子性包装：处理器对纸张聚合的变更与可能触发的开格必须同成同败。
   * 进入前快照整张纸；若开格回调（或处理器内任何步骤）抛错，整体回滚到快照，
   * 事件不写入去重表，修复硬件 / 回调后可用同一 eventId 安全重投。
   * 成功时提交变更，正常结果照常返回。
   */
  _withGateRollback(sheet, mutate) {
    const snapshot = structuredClone(sheet);
    try {
      return mutate(sheet);
    } catch (err) {
      this._restoreSheet(sheet, snapshot);
      throw err;
    }
  }

  _restoreSheet(sheet, snapshot) {
    for (const key of Object.keys(sheet)) {
      delete sheet[key];
    }
    Object.assign(sheet, structuredClone(snapshot));
  }

  _tryRelease(sheet, triggerEventId) {
    if (sheet.status !== SheetStatus.REGISTERED) {
      return { opened: false, reason: sheet.status };
    }
    if (sheet.deliveredAt !== null) {
      // 显式双保险：已交付纸绝不二次开格。
      return { opened: false, reason: 'ALREADY_DELIVERED' };
    }
    if (!this._isEvidenceComplete(sheet)) {
      return { opened: false, reason: 'EVIDENCE_INCOMPLETE' };
    }
    if (!this._gate) {
      throw reject(
        ErrorCode.GATE_FAILED,
        `纸 ${sheet.paperId} 证据齐备但未配置开格回调`,
        { paperId: sheet.paperId },
      );
    }

    const call = { at: this._clock(), triggerEventId, compartmentId: sheet.compartmentId };
    // 回调抛错时由 _withGateRollback 统一回滚整张纸（含本事件刚写入的证据），
    // 状态保持 REGISTERED、事件未入去重表，修复闸门后重投同一事件安全重试。
    try {
      this._gate.openCompartment(sheet.compartmentId, {
        paperId: sheet.paperId,
        ownerId: sheet.ownerId,
        jobId: sheet.jobId,
      });
    } catch (err) {
      throw reject(
        ErrorCode.GATE_FAILED,
        `开格回调失败：${err && err.message ? err.message : String(err)}`,
        { paperId: sheet.paperId, compartmentId: sheet.compartmentId, cause: err },
      );
    }

    sheet.gateCalls.push(call);
    sheet.status = SheetStatus.DELIVERED;
    sheet.deliveredAt = call.at;
    sheet.audit.push({ at: call.at, eventId: triggerEventId, type: 'COMPARTMENT_OPENED' });
    return { opened: true, at: call.at, compartmentId: sheet.compartmentId };
  }
}
