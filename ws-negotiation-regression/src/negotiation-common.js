/**
 * 协商公共逻辑：子协议选择与响应头序列化。
 * 子协议规则在两代策略间不变，统一在此实现（SP-1 ~ SP-3）。
 */

import { RULE, SOURCE } from './constants.js';

/**
 * 从客户端声明集合中选择子协议。
 * SP-1：按客户端声明顺序，取第一个服务端也支持的候选。
 * SP-2：有候选但无交集 → 不回应子协议头（允许无扩展裸连接）。
 * SP-3：客户端未声明任何子协议 → 仅回显协议版本，本记录中子协议为空。
 */
export function selectSubprotocol(offeredSubprotocols, capability) {
  const supported = new Set(capability.supportedSubprotocols);
  const candidateOrder = offeredSubprotocols.filter((p) => supported.has(p));

  if (offeredSubprotocols.length === 0) {
    return {
      selected: null,
      source: null,
      rule: 'SP-3',
      ruleText: RULE.SP3,
      candidateOrder: [],
      responseHeader: null,
    };
  }

  if (candidateOrder.length === 0) {
    return {
      selected: null,
      source: null,
      rule: 'SP-2',
      ruleText: RULE.SP2,
      candidateOrder: [],
      responseHeader: null,
    };
  }

  const selected = candidateOrder[0];
  return {
    selected,
    source: SOURCE.CLIENT_OFFER,
    rule: 'SP-1',
    ruleText: RULE.SP1,
    candidateOrder,
    responseHeader: `Sec-WebSocket-Protocol: ${selected}`,
  };
}

/** 按固定顺序序列化 permessage-deflate 响应头，保证回归产物可 diff */
export function serializePmdResponse({ flags = [], windows = {} } = {}) {
  const parts = ['permessage-deflate'];
  for (const flag of ['server_no_context_takeover', 'client_no_context_takeover']) {
    if (flags.includes(flag)) parts.push(flag);
  }
  for (const key of ['server_max_window_bits', 'client_max_window_bits']) {
    if (windows[key] !== undefined) parts.push(`${key}=${windows[key]}`);
  }
  return `Sec-WebSocket-Extensions: ${parts.join('; ')}`;
}
