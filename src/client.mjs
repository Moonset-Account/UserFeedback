// 带槽表缓存的智能客户端（单进程内存版，同步调用）。
//
// 路由行为（与 Redis Cluster 客户端约定一致）：
//   ASK     -> 槽表不动；向 A 索要一次性 ASK 指示，只重试这一次
//   MOVED   -> 更新本地槽表，再按新槽主重试；后续请求直接走新节点
//   TRYAGAIN-> 多键读横跨搬迁两侧，原样上抛，不返回部分值，由调用者稍后重试
// 每条结果信封都带 routing 轨迹，调用者可区分「只重试一次」与「改了槽表」。

import { keySlot } from './hash.mjs';
import {
  AskRedirect,
  AskTicket,
  CrossSlotError,
  MovedRedirect,
  TryAgain,
  WrongSlotStateError,
} from './protocol.mjs';

const MAX_ATTEMPTS = 4;

export class SmartClient {
  #cluster;
  #slotTable;
  #stats = { requests: 0, askRetries: 0, movedRedirects: 0, tryAgain: 0 };

  /**
   * @param {import('./cluster.mjs').Cluster} cluster
   * @param {Map<number,string>} [initialSlotTable] 客户端缓存的旧槽表
   */
  constructor(cluster, initialSlotTable) {
    this.#cluster = cluster;
    this.#slotTable = new Map(initialSlotTable ?? cluster.slotTableSnapshot());
  }

  get stats() {
    return { ...this.#stats };
  }

  /** 客户端当前缓存的槽表（副本）。 */
  cachedSlotTable() {
    return new Map(this.#slotTable);
  }

  cachedOwner(slot) {
    return this.#slotTable.get(slot);
  }

  get(key) {
    const slot = keySlot(key);
    return this.#dispatch({ op: 'get', slot, key });
  }

  set(key, value) {
    const slot = keySlot(key);
    return this.#dispatch({ op: 'set', slot, key, value });
  }

  remove(key) {
    const slot = keySlot(key);
    return this.#dispatch({ op: 'delete', slot, key });
  }

  /**
   * 多键读。所有键必须同槽（否则 CROSSSLOT）。
   * 搬迁期间若两侧都有归属，节点返回 TRYAGAIN，这里原样上抛、不给部分值。
   */
  multiGet(keys) {
    if (keys.length === 0) throw new TypeError('multiGet 至少需要一个键');
    const slots = new Set(keys.map((k) => keySlot(k)));
    if (slots.size > 1) throw new CrossSlotError(slots);
    const slot = slots.values().next().value;
    return this.#dispatch({ op: 'mget', slot, keys: [...keys] });
  }

  #dispatch(initialRequest) {
    this.#stats.requests += 1;
    const { slot } = initialRequest;
    const routing = { attempts: [], slotTableUpdated: false, redirectedOnce: false };
    let request = initialRequest;
    // ASK 一次性通道：非空时下一跳绕过槽表直发 B，用一次即清空
    let oneShotTarget = null;

    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
      const nodeId = oneShotTarget ?? this.#slotTable.get(slot);
      if (!nodeId) throw new WrongSlotStateError(`客户端槽表缺少槽 ${slot}`);
      const node = this.#cluster.getNode(nodeId);
      const trace = { attempt, nodeId, op: request.op, outcome: 'ok', via: oneShotTarget ? 'ASK-channel' : 'slot-table' };

      let result;
      try {
        result = node.handle(request);
      } catch (err) {
        if (err instanceof AskRedirect) {
          if (oneShotTarget) {
            throw new Error('协议错误：ASK 一次性通道内不得再次触发 ASK');
          }
          trace.outcome = 'ASK';
          trace.target = err.targetNodeId;
          routing.attempts.push(trace);
          routing.redirectedOnce = true;
          this.#stats.askRetries += 1;
          // 向当前缓存槽主 A 索要一次性指示，槽表保持不变
          const ticket = this.#cluster.getNode(nodeId).issueAskTicket(slot);
          request = { ...request, ticket };
          oneShotTarget = err.targetNodeId; // 下一轮直发 B，只生效一轮
          continue;
        }
        if (err instanceof MovedRedirect) {
          trace.outcome = 'MOVED';
          trace.target = err.targetNodeId;
          routing.attempts.push(trace);
          routing.slotTableUpdated = true;
          this.#stats.movedRedirects += 1;
          this.#slotTable.set(slot, err.targetNodeId); // 永久更新缓存
          oneShotTarget = null;
          request = { ...request };
          delete request.ticket;
          continue;
        }
        if (err instanceof TryAgain) {
          trace.outcome = 'TRYAGAIN';
          routing.attempts.push(trace);
          this.#stats.tryAgain += 1;
          err.routing = routing;
          throw err;
        }
        throw err;
      }

      // 一次性通道无论成功与否都立即关闭，绝不残留为隐式路由
      oneShotTarget = null;
      routing.attempts.push(trace);
      return { ...result, routing };
    }
    throw new Error(`路由重试超过 ${MAX_ATTEMPTS} 次，疑似重定向循环`);
  }
}
