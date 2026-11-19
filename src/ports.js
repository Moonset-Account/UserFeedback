/**
 * @typedef {object} OutputSensor 出纸传感器端口（硬件可替换）
 * @property {string} sensorId 传感器硬件编号
 * @property {(paperId: string) => object} observe 模拟一次物理出纸观测，返回原始事件载荷
 *
 * @typedef {object} CompartmentGate 取件格闸门端口（开格回调可替换）
 * @property {(compartmentId: string, context: object) => void} openCompartment
 */

/**
 * 可替换的出纸传感器测试/默认实现。
 * 真实环境中可换成串口 / GPIO 驱动，只要实现同样的 sensorId 与 observe 契约。
 */
export class OutputSensor {
  /**
   * @param {string} sensorId
   * @param {object} [options]
   * @param {(paperId: string) => boolean} [options.shouldObserve] 故障注入：返回 false 表示传感器漏报
   */
  constructor(sensorId, options = {}) {
    if (typeof sensorId !== 'string' || sensorId.length === 0) {
      throw new Error('OutputSensor 需要非空 sensorId');
    }
    this.sensorId = sensorId;
    this._shouldObserve = options.shouldObserve ?? (() => true);
    this.observed = [];
  }

  /**
   * 观测到某张纸物理出纸。返回事件字段；漏报时返回 null（调用方据此决定不上报）。
   */
  observe(paperId) {
    if (!this._shouldObserve(paperId)) {
      return null;
    }
    this.observed.push(paperId);
    return { paperId, sensorId: this.sensorId };
  }
}

/**
 * 可替换的开格回调：记录每次开格请求。
 * 支持故障注入（前 failTimes 次抛错），用于验证重试与不重复开格。
 */
export class RecordingGate {
  /**
   * @param {object} [options]
   * @param {number} [options.failTimes] 前 N 次 openCompartment 调用抛错
   * @param {(compartmentId: string, context: object) => void} [options.onOpen] 每次成功开格的观察钩子
   */
  constructor(options = {}) {
    this.calls = [];
    this._failRemaining = options.failTimes ?? 0;
    this._onOpen = options.onOpen ?? null;
  }

  openCompartment(compartmentId, context) {
    if (this._failRemaining > 0) {
      this._failRemaining -= 1;
      throw new Error(`闸门 ${compartmentId} 机械故障（注入）`);
    }
    this.calls.push({ compartmentId, context: { ...context }, at: Date.now() });
    if (this._onOpen) {
      this._onOpen(compartmentId, context);
    }
  }

  /** 实际成功开格次数（跨控制器重放应始终 ≤ 每张纸 1 次）。 */
  get openCount() {
    return this.calls.length;
  }
}
