// 会话输出组件
//
// 场景：推理端沿一条「提议链」连续送审草稿 Token，每段从上一段草稿末尾开始；
// 验证端逐段回复接受的前缀长度，未全收时可再给一个替换 Token。
//
// 不变式：
//   1. 子段可以先到，但只有父段被「全数原样确认」后，子段才能提交（进入确定输出）。
//   2. 父段被截短或纠正：发出确认前缀 + 替换项（若有），作废全部旧后继，
//      并允许从纠正后的已提交位置另起一条链。
//   3. 确认序列遇到停止 Token 即终止：停止符及其后的内容一律不发。
//   4. 客户端重连时报告已收 Token 数，只补余下的确定内容。
//
// Token 一律为整数；停止符默认为 0，可在构造函数配置。

import { EventEmitter } from 'node:events';

const STATUS = Object.freeze({
  PENDING: 'pending',         // 已提交，验证回复未到（或被缓冲，等待父段落定）
  CONFIRMED: 'confirmed',     // 全数原样确认，且内容已提交
  CORRECTED: 'corrected',     // 被截短/纠正，仅前缀（+替换项）已提交，后继全部作废
  STOPPED: 'stopped',         // 提交内容在本段遇到停止符，会话终止
  INVALIDATED: 'invalidated', // 因祖先被纠正而作废
});

function assertIntegerToken(value, label) {
  if (typeof value !== 'number' || !Number.isSafeInteger(value)) {
    throw new TypeError(`${label} 必须是安全整数 Token，收到: ${String(value)}`);
  }
}

export class SessionOutput extends EventEmitter {
  #segments = new Map();        // id -> 段记录
  #children = new Map();        // 父 id -> [子 id...]（含已作废的子段）
  #roots = [];                  // 根段 id 顺序
  #activeRootId = null;         // 当前挂在提交头上的有效链的根
  #committed = [];              // 确定输出 Token 序列（不含停止符）
  #stopToken;
  #terminated = false;          // 是否已遇停止符
  #events = [];                 // 事件日志，便于诊断/测试

  /**
   * @param {object} [options]
   * @param {number} [options.stopToken=0] 停止 Token（整数）
   */
  constructor(options = {}) {
    super();
    const stopToken = options.stopToken ?? 0;
    assertIntegerToken(stopToken, 'stopToken');
    this.#stopToken = stopToken;
  }

  /** 确定输出 Token 序列的只读副本 */
  get committedTokens() {
    return [...this.#committed];
  }

  /** 已提交确定 Token 数，即「已提交位置」/链头位置 */
  get head() {
    return this.#committed.length;
  }

  /** 是否已遇停止 Token 终止 */
  get terminated() {
    return this.#terminated;
  }

  /** 停止 Token */
  get stopToken() {
    return this.#stopToken;
  }

  /** 事件日志的只读副本（按发生顺序） */
  get eventLog() {
    return this.#events.map((entry) => ({ ...entry }));
  }

  /** 段记录的只读副本；段不存在时返回 undefined */
  getSegment(id) {
    const seg = this.#segments.get(id);
    return seg ? { ...seg, tokens: [...seg.tokens] } : undefined;
  }

  /** 当前挂在提交头上的有效链的根段 id；没有待处理链时为 null */
  get activeRootId() {
    return this.#activeRootId;
  }

  /**
   * 提交一段草稿。
   * @param {object} params
   * @param {string|number} params.id            段唯一标识
   * @param {number[]} params.tokens             草稿 Token（非空整数数组）
   * @param {string|number|null} [params.parentId] 父段 id；省略/null 表示根段
   * @returns {object} 段记录副本
   */
  submitSegment({ id, tokens, parentId = null }) {
    if (id === undefined || id === null) {
      throw new TypeError('submitSegment 缺少段 id');
    }
    if (this.#segments.has(id)) {
      throw new Error(`段 id 重复: ${String(id)}`);
    }
    if (!Array.isArray(tokens) || tokens.length === 0) {
      throw new TypeError('tokens 必须是非空整数数组');
    }
    tokens.forEach((t, i) => assertIntegerToken(t, `tokens[${i}]`));

    let start;
    if (parentId === null || parentId === undefined) {
      // 根段：只能在没有挂在提交头上的有效提议链时另起
      if (this.#activeRootId !== null) {
        throw new Error(
          `已有活动链（根段 ${String(this.#activeRootId)}）尚未落定，不能在提交头另起根段`,
        );
      }
      if (this.#terminated) {
        throw new Error('会话已因停止 Token 终止，不能再提交草稿');
      }
      start = this.head;
      this.#roots.push(id);
      this.#activeRootId = id;
    } else {
      const parent = this.#segments.get(parentId);
      if (!parent) {
        throw new Error(`父段不存在: ${String(parentId)}`);
      }
      if (parent.status === STATUS.INVALIDATED) {
        throw new Error(`父段 ${String(parentId)} 已作废，不能在其上追加子段`);
      }
      if (parent.status === STATUS.CORRECTED || parent.status === STATUS.STOPPED) {
        throw new Error(
          `父段 ${String(parentId)} 已被纠正/终止，旧链已断，应从当前提交头另起根段`,
        );
      }
      // 每段同一时刻只允许一条有效后继
      const activeChild = this.#activeChildId(parentId);
      if (activeChild !== null) {
        throw new Error(
          `父段 ${String(parentId)} 已存在有效后继 ${String(activeChild)}`,
        );
      }
      // 每段从上一段草稿的末尾开始
      start = parent.start + parent.tokens.length;
      if (!this.#children.has(parentId)) this.#children.set(parentId, []);
      this.#children.get(parentId).push(id);
    }

    const record = {
      id,
      parentId: parentId ?? null,
      start,
      tokens: [...tokens],
      status: STATUS.PENDING,
      bufferedResult: null,   // 验证回复先于父段落定时的缓冲
      emittedTokens: [],      // 本段实际进入确定输出的 Token
      replacement: null,      // 纠正时给出的替换 Token
    };
    this.#segments.set(id, record);

    // 子回复可能先到：缓冲的验证结果此时可以尝试落定
    this.#pump();
    return this.getSegment(id);
  }

  /**
   * 送入验证端对某段的回复。
   * @param {object} params
   * @param {string|number} params.id            段 id
   * @param {number} params.acceptedPrefixLength 接受的前缀长度（0..tokens.length）
   * @param {number} [params.replacementToken]   未全收时可选的一个替换 Token
   * @returns {object} 处理后的段记录副本
   */
  verifySegment({ id, acceptedPrefixLength, replacementToken }) {
    const seg = this.#segments.get(id);
    if (!seg) {
      throw new Error(`段不存在: ${String(id)}`);
    }
    if (seg.status !== STATUS.PENDING) {
      throw new Error(`段 ${String(id)} 状态为 ${seg.status}，不能重复验证`);
    }
    if (
      typeof acceptedPrefixLength !== 'number' ||
      !Number.isSafeInteger(acceptedPrefixLength) ||
      acceptedPrefixLength < 0 ||
      acceptedPrefixLength > seg.tokens.length
    ) {
      throw new RangeError(
        `acceptedPrefixLength 必须是 0..${seg.tokens.length} 之间的整数`,
      );
    }
    const fullyAccepted = acceptedPrefixLength === seg.tokens.length;
    if (replacementToken !== undefined && replacementToken !== null) {
      assertIntegerToken(replacementToken, 'replacementToken');
      if (fullyAccepted) {
        throw new Error('全数原样接受时不允许携带替换 Token');
      }
    }

    const result = {
      acceptedPrefixLength,
      replacementToken: replacementToken ?? null,
    };

    // 父段尚未落定：先缓冲，等父段落定后再处理（子回复先到的情形）
    if (seg.parentId !== null) {
      const parent = this.#segments.get(seg.parentId);
      if (parent.status === STATUS.PENDING) {
        seg.bufferedResult = result;
        return this.getSegment(id);
      }
    }

    this.#applyResult(seg, result);
    this.#pump();
    return this.getSegment(id);
  }

  /**
   * 重连补传：客户端报告已收 Token 数，只返回余下的确定内容。
   * @param {number} receivedCount 客户端已收 Token 数
   * @returns {{tokens: number[], from: number, to: number, terminated: boolean, caughtUp: boolean}}
   */
  replay(receivedCount) {
    if (
      typeof receivedCount !== 'number' ||
      !Number.isSafeInteger(receivedCount) ||
      receivedCount < 0
    ) {
      throw new TypeError('receivedCount 必须是非负整数');
    }
    if (receivedCount > this.head) {
      throw new RangeError(
        `receivedCount=${receivedCount} 超过已提交长度 ${this.head}，无法补传`,
      );
    }
    const tokens = this.#committed.slice(receivedCount);
    return {
      tokens,
      from: receivedCount,
      to: this.head,
      terminated: this.#terminated,
      caughtUp: receivedCount === this.head,
    };
  }

  // ---- 内部实现 -----------------------------------------------------------

  /** 取某父段当前仍有效的子段 id（作废的不算） */
  #activeChildId(parentId) {
    const kids = this.#children.get(parentId);
    if (!kids) return null;
    for (const kidId of kids) {
      const kid = this.#segments.get(kidId);
      if (kid && kid.status !== STATUS.INVALIDATED) return kidId;
    }
    return null;
  }

  /**
   * 从活动链根出发，沿有效后继推进，处理所有父段已落定的待处理段。
   */
  #pump() {
    let id = this.#activeRootId;
    while (id !== null) {
      const seg = this.#segments.get(id);
      if (!seg || seg.status === STATUS.INVALIDATED) return;
      if (seg.status === STATUS.PENDING) {
        if (seg.bufferedResult) {
          const result = seg.bufferedResult;
          seg.bufferedResult = null;
          this.#applyResult(seg, result);
          continue; // 纠正会换根/终止，重新读取活动根
        }
        return; // 链头仍在等验证回复，后面的子段即使有回复也必须继续等
      }
      // 已落定：全数确认则继续向子段推进；纠正/停止后链已断
      if (seg.status === STATUS.CONFIRMED) {
        id = this.#activeChildId(id);
      } else {
        return;
      }
    }
    // 走到链尖且无更多有效后继：整条链已落定，释放提交头
    this.#activeRootId = null;
  }

  /** 对一段套用验证回复，发出确定内容并维护链的状态 */
  #applyResult(seg, result) {
    const { acceptedPrefixLength, replacementToken } = result;
    const prefix = seg.tokens.slice(0, acceptedPrefixLength);
    const fullyAccepted = acceptedPrefixLength === seg.tokens.length;

    // 停止符检查：停止符及其后的内容都不发
    const stopIndex = prefix.indexOf(this.#stopToken);
    if (stopIndex !== -1) {
      const beforeStop = prefix.slice(0, stopIndex);
      this.#append(beforeStop, seg, 'segment');
      seg.status = STATUS.STOPPED;
      this.#invalidateDescendants(seg.id);
      this.#terminated = true;
      this.#activeRootId = null;
      this.#recordAndEmit('stopped', {
        segmentId: seg.id,
        tokens: [...beforeStop],
        stopToken: this.#stopToken,
      });
      return;
    }

    // 发出确认前缀（全收时即整段原样确认）
    this.#append(prefix, seg, 'segment');

    if (fullyAccepted) {
      seg.status = STATUS.CONFIRMED;
      this.#recordAndEmit('committed', {
        segmentId: seg.id,
        tokens: [...prefix],
      });
      return;
    }

    // 未全收：截短/纠正，发出替换项（停止符替换项同样不发，并终止会话）
    seg.replacement = replacementToken;
    if (replacementToken !== null && replacementToken !== this.#stopToken) {
      this.#append([replacementToken], seg, 'replacement');
    }

    if (replacementToken === this.#stopToken) {
      seg.status = STATUS.STOPPED;
      this.#terminated = true;
      this.#activeRootId = null;
      this.#recordAndEmit('stopped', {
        segmentId: seg.id,
        tokens: [],
        stopToken: this.#stopToken,
      });
    } else {
      seg.status = STATUS.CORRECTED;
      // 允许从纠正后的已提交位置另起一条链
      this.#activeRootId = null;
    }

    // 先宣布纠正，再作废全部旧后继
    this.#recordAndEmit('corrected', {
      segmentId: seg.id,
      acceptedPrefix: [...prefix],
      replacementToken,
    });
    this.#invalidateDescendants(seg.id);
  }

  /** 作废一段的全部旧后继（子树），返回作废的 id 列表 */
  #invalidateDescendants(parentId) {
    const invalidated = [];
    const queue = [parentId];
    while (queue.length > 0) {
      const current = queue.shift();
      const kids = this.#children.get(current) ?? [];
      for (const kidId of kids) {
        const kid = this.#segments.get(kidId);
        if (kid && kid.status !== STATUS.INVALIDATED) {
          kid.status = STATUS.INVALIDATED;
          kid.bufferedResult = null;
          invalidated.push(kidId);
          queue.push(kidId);
        }
      }
    }
    if (invalidated.length > 0) {
      this.#recordAndEmit('invalidated', { segmentIds: invalidated });
    }
    return invalidated;
  }

  /** 把 Token 追加到确定输出，并记录在段的 emittedTokens 上 */
  #append(tokens, seg, kind) {
    if (tokens.length === 0) return;
    this.#committed.push(...tokens);
    seg.emittedTokens.push(...tokens);
    this.#recordAndEmit('tokens', {
      segmentId: seg.id,
      kind, // 'segment' | 'replacement'
      tokens: [...tokens],
    });
  }

  #recordAndEmit(type, payload) {
    const entry = { type, ...payload };
    this.#events.push(entry);
    this.emit(type, entry);
  }
}

export { STATUS as SegmentStatus };
export default SessionOutput;
