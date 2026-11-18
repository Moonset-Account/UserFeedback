/**
 * 确定性逻辑时钟。
 *
 * 两个相互独立的量：
 *  - next()：全局严格单调序列号，作为决策/审批/凭证/撤销的稳定排序依据；
 *  - wallMs()/now()：墙上时间（毫秒/ISO），用于租约有效窗口判断。
 *
 * 真实节点可注入包装 Date.now() 的实现；测试中使用 MutableClock 手动拨表。
 * 记录时间戳来自 now()，与序列号解耦，避免“每调用一次时钟就前进”的耦合。
 */
export class LogicalClock {
  #seq = 0;
  #baseMs;

  constructor(baseTime = '2026-11-19T00:00:00.000Z') {
    const t = Date.parse(baseTime);
    if (Number.isNaN(t)) {
      throw new TypeError(`非法基准时间: ${baseTime}`);
    }
    this.#baseMs = t;
  }

  /** 推进并返回全局单调序列号（决策/审批/凭证序号来源）。 */
  next() {
    this.#seq += 1;
    return this.#seq;
  }

  get sequence() {
    return this.#seq;
  }

  /** 墙上时间（毫秒）。默认实现锚定基准时间，测试可子类化拨表。 */
  wallMs() {
    return this.#baseMs;
  }

  now() {
    return new Date(this.wallMs()).toISOString();
  }
}
