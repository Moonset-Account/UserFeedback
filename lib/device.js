/**
 * 模拟认证设备：为测试与演示生成"已校验"的成员事件。
 *
 * 这是测试夹具而非真实认证器：不读取、不存储任何凭据，只按单调递增的
 * 接入编号（accessNo）合成进出报告与认证确认。每个终端维护独立的编号序列。
 */

import { createAuthEvent, createPresenceEvent, PRESENCE_STATUS } from './events.js';

export class SimulatedAuthDevice {
  /**
   * @param {object} [options]
   * @param {() => number} [options.clock] 时间源，默认 Date.now，测试可注入虚拟时钟。
   */
  constructor({ clock = () => Date.now() } = {}) {
    if (typeof clock !== 'function') {
      throw new TypeError('clock 必须是返回毫秒时间戳的函数');
    }
    this.#clock = clock;
    /** @type {Map<string, number>} 每终端下一个接入编号 */
    this.#nextAccessNo = new Map();
  }

  #clock;
  #nextAccessNo;

  #advance(terminalId) {
    const next = (this.#nextAccessNo.get(terminalId) ?? 0) + 1;
    this.#nextAccessNo.set(terminalId, next);
    return next;
  }

  #at(terminalId, timestamp) {
    return timestamp === undefined ? this.#clock() : timestamp;
  }

  /** 成员进入终端感应区。 */
  enter(memberId, terminalId, timestamp) {
    return createPresenceEvent({
      memberId,
      terminalId,
      timestamp: this.#at(terminalId, timestamp),
      accessNo: this.#advance(terminalId),
      status: PRESENCE_STATUS.IN,
    });
  }

  /** 成员离开终端感应区。 */
  leave(memberId, terminalId, timestamp) {
    return createPresenceEvent({
      memberId,
      terminalId,
      timestamp: this.#at(terminalId, timestamp),
      accessNo: this.#advance(terminalId),
      status: PRESENCE_STATUS.OUT,
    });
  }

  /** 成员在认证设备上完成确认。 */
  authenticate(memberId, terminalId, timestamp) {
    return createAuthEvent({
      memberId,
      terminalId,
      timestamp: this.#at(terminalId, timestamp),
      accessNo: this.#advance(terminalId),
    });
  }

  /** 查询终端当前的下一个接入编号（测试断言用）。 */
  nextAccessNo(terminalId) {
    return (this.#nextAccessNo.get(terminalId) ?? 0) + 1;
  }
}
