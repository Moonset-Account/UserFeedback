/**
 * 可注入时钟。
 *
 * 校验器不直接读取系统时间，而是依赖 Clock.now()，
 * 这样回归测试可以确定性地推进时间，精确覆盖宽限期边界。
 */
export class Clock {
  #nowMs;

  /**
   * @param {number} [startMs=0] 初始时间（毫秒，单调递增的虚拟时间轴）
   */
  constructor(startMs = 0) {
    this.#nowMs = startMs;
  }

  /** 当前虚拟时间（毫秒） */
  now() {
    return this.#nowMs;
  }

  /**
   * 向前推进时间。
   * @param {number} deltaMs 非负毫秒数
   * @returns {number} 推进后的时间
   */
  advance(deltaMs) {
    if (!Number.isFinite(deltaMs) || deltaMs < 0) {
      throw new TypeError(`advance 需要非负毫秒数，收到: ${deltaMs}`);
    }
    this.#nowMs += deltaMs;
    return this.#nowMs;
  }

  /** 直接设置时间（仅允许向前，保证单调性） */
  set(ms) {
    if (!Number.isFinite(ms) || ms < this.#nowMs) {
      throw new TypeError(`set 只能向前设置时间，当前=${this.#nowMs}，收到=${ms}`);
    }
    this.#nowMs = ms;
    return this.#nowMs;
  }
}
