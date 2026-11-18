// 网关客户端地址解析：基于可信代理链，从连接对端逐跳向前核对。
import { parseIp, matchCidr } from './ip.mjs';

export const REASONS = Object.freeze({
  INVALID_PEER: 'invalid-peer-address',
  MALFORMED_CHAIN: 'malformed-forwarded-chain',
  HOP_LIMIT_EXCEEDED: 'hop-limit-exceeded',
});

const DEFAULT_MAX_HOPS = 16;
const DEFAULT_ON_INVALID = 'reject';

/**
 * 规范化入口配置。
 * entry: {
 *   name?: string,
 *   trustedProxies?: string[],   // IP 或 CIDR 列表
 *   maxHops?: number,            // 转发链允许的最大跳数（默认 16）
 *   onInvalid?: 'reject'|'fallback' // 链格式错误或超跳数时的策略（默认 reject）
 * }
 */
export function normalizeEntry(entry = {}) {
  const trustedProxies = Array.isArray(entry.trustedProxies) ? entry.trustedProxies : [];
  const maxHops = Number.isInteger(entry.maxHops) && entry.maxHops >= 0
    ? entry.maxHops
    : DEFAULT_MAX_HOPS;
  const onInvalid = entry.onInvalid === 'fallback' ? 'fallback' : DEFAULT_ON_INVALID;
  return {
    name: typeof entry.name === 'string' ? entry.name : 'default',
    trustedProxies,
    maxHops,
    onInvalid,
  };
}

function isTrusted(ip, trustedProxies) {
  return trustedProxies.some((cidr) => matchCidr(ip, cidr));
}

function connectionEvidence(reason, extra = {}) {
  return {
    source: 'connection',
    reason,
    ...extra,
  };
}

/**
 * 解析客户端地址。
 *
 * @param {object} input
 * @param {string} input.remoteAddress  实际连接对端地址（必需）
 * @param {string} [input.forwardedFor] 请求声明的转发链（X-Forwarded-For 原始值）
 * @param {object} [input.entry]        入口的可信代理配置
 * @returns {{
 *   status: 'resolved'|'rejected',
 *   clientAddress: string|null,
 *   evidence: object
 * }}
 */
export function resolveClientAddress(input) {
  const { remoteAddress, forwardedFor } = input ?? {};
  const entry = normalizeEntry(input?.entry);

  // 1. 连接对端必须是合法 IP，否则无法建立任何证据
  if (!parseIp(remoteAddress)) {
    return {
      status: 'rejected',
      clientAddress: null,
      evidence: connectionEvidence(REASONS.INVALID_PEER, {
        entry: entry.name,
        remoteAddress: remoteAddress ?? null,
      }),
    };
  }

  // 2. 未声明转发链：直连，使用连接对端
  if (forwardedFor === undefined || forwardedFor === null || String(forwardedFor).trim() === '') {
    return {
      status: 'resolved',
      clientAddress: remoteAddress,
      evidence: connectionEvidence('direct-connection', { entry: entry.name }),
    };
  }

  // 3. 解析转发链；格式错误按入口策略拒绝或回退
  const tokens = String(forwardedFor).split(',').map((t) => t.trim());
  const malformed = tokens.some((t) => !parseIp(t));
  if (malformed) {
    return handleInvalid(entry, REASONS.MALFORMED_CHAIN, remoteAddress, {
      declaredChain: tokens,
    });
  }

  // 4. 跳数超限按入口策略拒绝或回退
  if (tokens.length > entry.maxHops) {
    return handleInvalid(entry, REASONS.HOP_LIMIT_EXCEEDED, remoteAddress, {
      declaredChain: tokens,
      maxHops: entry.maxHops,
    });
  }

  // 5. 连接对端不受信：链不是经可信入口声明的，不参与判断，使用连接对端
  if (!isTrusted(remoteAddress, entry.trustedProxies)) {
    return {
      status: 'resolved',
      clientAddress: remoteAddress,
      evidence: connectionEvidence('peer-not-trusted-chain-ignored', {
        entry: entry.name,
        declaredChain: tokens,
      }),
    };
  }

  // 6. 从连接对端逐跳向前（链尾向链首）核对，直到首个不受信的跳
  const trustedHops = [remoteAddress];
  let clientIndex = tokens.length - 1;
  while (clientIndex >= 0 && isTrusted(tokens[clientIndex], entry.trustedProxies)) {
    trustedHops.push(tokens[clientIndex]);
    clientIndex -= 1;
  }
  // clientIndex < 0 表示整条链都受信，客户端取链首声明
  const clientAddress = clientIndex < 0 ? tokens[0] : tokens[clientIndex];

  return {
    status: 'resolved',
    clientAddress,
    evidence: {
      source: 'forwarded-chain',
      entry: entry.name,
      declaredChain: tokens,
      trustedHops,
      untrustedHop: clientIndex < 0 ? null : tokens[clientIndex],
      usedSegment: clientIndex < 0 ? [tokens[0]] : tokens.slice(0, clientIndex + 1),
    },
  };
}

function handleInvalid(entry, reason, remoteAddress, extra) {
  if (entry.onInvalid === 'fallback') {
    return {
      status: 'resolved',
      clientAddress: remoteAddress,
      evidence: connectionEvidence(`fallback:${reason}`, {
        entry: entry.name,
        ...extra,
      }),
    };
  }
  return {
    status: 'rejected',
    clientAddress: null,
    evidence: {
      source: 'forwarded-chain',
      entry: entry.name,
      reason,
      ...extra,
    },
  };
}
