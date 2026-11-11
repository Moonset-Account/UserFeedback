/**
 * 服务端能力表（Server Capability Table）。
 *
 * 两份快照对应同一服务的两代策略：
 *  - CAPABILITY_V1：旧策略。配置分支（configured）有预算约束，但默认分支（default）
 *    走遗留回退，不携带任何资源预算 —— 这正是历史缺陷的根因。
 *  - CAPABILITY_V2：修正策略。所有路由分支共享同一份资源预算与参数合法区间，
 *    默认分支不再存在无约束回退路径。
 *
 * 资源预算语义：
 *  - maxWindowBits：服务端消息处理为 LZ77 滑动窗口预留的最大窗口位数，
 *    决定每个压缩上下文的内存占用（2^windowBits 量级）。
 *  - maxDecompressedMessageBytes：单条消息解压后的最大字节数，
 *    扩展一旦启用就必须与该预算绑定，防止解压炸弹耗尽内存。
 *  - enforceNoContextTakeover：禁止跨消息保留压缩上下文，把每消息状态
 *    限制在预算内（“消息处理预设的资源预算”）。
 */

import { RFC_WINDOW_BITS_MAX, RFC_WINDOW_BITS_MIN } from './constants.js';

/** 公共资源预算：v1 配置分支与 v2 全部分支共用 */
const RESOURCE_BUDGET = Object.freeze({
  maxWindowBits: 12,
  maxDecompressedMessageBytes: 1_048_576, // 1 MiB
  enforceNoContextTakeover: true,
});

/**
 * 策略规定的窗口位数合法区间 = RFC 协议区间 ∩ 服务端预算区间。
 * v2 明确把该交集写进能力表；v1 仅配置分支生效。
 */
const POLICY_WINDOW_RANGE = Object.freeze({
  min: RFC_WINDOW_BITS_MIN, // 8
  max: RESOURCE_BUDGET.maxWindowBits, // 12
});

export const CAPABILITY_V1 = Object.freeze({
  policyVersion: 'v1-legacy',
  description: '旧策略：配置分支受预算约束，默认分支为遗留回退（缺失资源限制）',
  supportedSubprotocols: Object.freeze(['mqtt', 'chat.v2', 'wamp']),
  allowNoSubprotocol: true,
  extensions: Object.freeze({
    'permessage-deflate': Object.freeze({
      enabled: true,
      rfcWindowBitsRange: Object.freeze({
        min: RFC_WINDOW_BITS_MIN,
        max: RFC_WINDOW_BITS_MAX,
      }),
      branches: Object.freeze({
        // 显式配置的路由：预算约束完整
        configured: Object.freeze({
          resourceBudget: RESOURCE_BUDGET,
          policyWindowBitsRange: POLICY_WINDOW_RANGE,
          enforceNoContextTakeover: true,
          bindDecompressedBudget: true,
        }),
        // 默认分支：历史缺陷点 —— 无预算、无钳制、无强制项、无绑定
        default: Object.freeze({
          resourceBudget: null,
          policyWindowBitsRange: Object.freeze({
            min: RFC_WINDOW_BITS_MIN,
            max: RFC_WINDOW_BITS_MAX,
          }),
          enforceNoContextTakeover: false,
          bindDecompressedBudget: false,
        }),
      }),
    }),
  }),
  otherExtensions: Object.freeze([
    // 服务端未实现/未启用的扩展，一律按 EX-1 省略
    Object.freeze({ name: 'x-supercompress', enabled: false }),
  ]),
});

export const CAPABILITY_V2 = Object.freeze({
  policyVersion: 'v2-fixed',
  description: '修正策略：默认分支与配置分支统一绑定资源预算，新增声明完整性闸门',
  supportedSubprotocols: Object.freeze(['mqtt', 'chat.v2', 'wamp']),
  allowNoSubprotocol: true,
  extensions: Object.freeze({
    'permessage-deflate': Object.freeze({
      enabled: true,
      rfcWindowBitsRange: Object.freeze({
        min: RFC_WINDOW_BITS_MIN,
        max: RFC_WINDOW_BITS_MAX,
      }),
      // v2：分支字段仅保留路由名，预算对所有分支统一生效
      uniformResourceBudget: RESOURCE_BUDGET,
      policyWindowBitsRange: POLICY_WINDOW_RANGE,
      enforceNoContextTakeover: true,
      bindDecompressedBudget: true,
      completenessGate: Object.freeze({
        enabled: true,
        // 启用协商时必须显式给出的参数（缺一项即 PENDING_CONFIRMATION）
        requiredPresent: Object.freeze([
          'client_max_window_bits',
          'server_max_window_bits',
        ]),
      }),
    }),
  }),
  otherExtensions: Object.freeze([
    Object.freeze({ name: 'x-supercompress', enabled: false }),
  ]),
});

export { RESOURCE_BUDGET, POLICY_WINDOW_RANGE };
