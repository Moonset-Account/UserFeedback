/**
 * 可注入时钟，默认实现读取真实系统时间。
 * 核验逻辑本身不直接调用 Date.now()，而是经由时钟对象，
 * 这样测试可以注入固定时钟，保证结果完全确定。
 */
import { toTime } from './time.mjs';

export class Clock {
  #nowMillis;

  /**
   * @param {number|string|Date} [fixedTime] 提供后时钟固定在该时刻，用于测试。
   */
  constructor(fixedTime) {
    if (fixedTime !== undefined) {
      this.#nowMillis = toTime(fixedTime, 'fixedTime');
    } else {
      this.#nowMillis = null;
    }
  }

  now() {
    return this.#nowMillis === null ? Date.now() : this.#nowMillis;
  }
}
