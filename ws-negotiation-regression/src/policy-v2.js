/**
 * 修正策略 v2 协商引擎。
 *
 * 相对 v1 的两处修复：
 *  1) PM-6 落地：删除无约束的遗留回退，默认分支与配置分支走同一条预算绑定路径，
 *     窗口钳制(PM-1/PM-2)、强制 no_context_takeover(PM-3)、解压预算绑定(PM-4) 全部生效；
 *  2) 新增 PM-5 完整性闸门：客户端声明缺项时不再静默套用 RFC 缺省，
 *     而是单列 PENDING_CONFIRMATION，要求补全声明后重新协商。
 */

import {
  PMD,
  RFC_WINDOW_BITS_MAX,
  RFC_WINDOW_BITS_MIN,
  RULE,
  SOURCE,
  VERDICT,
} from './constants.js';
import { selectSubprotocol, serializePmdResponse } from './negotiation-common.js';

const RFC_RANGE = Object.freeze({ min: RFC_WINDOW_BITS_MIN, max: RFC_WINDOW_BITS_MAX });

export function negotiateV2(parsed, capability) {
  const extCap = capability.extensions['permessage-deflate'];
  const offer = parsed.extensionOffers.find((o) => o.name === 'permessage-deflate') ?? null;
  const extensionResults = [];

  if (offer?.malformed) {
    extensionResults.push({
      name: 'permessage-deflate',
      verdict: VERDICT.REJECT,
      responseHeader: null,
      rules: ['EX-2'],
      ruleTexts: [RULE.EX2],
      parameters: [],
      resourceBudget: null,
      budgetBinding: { bound: false, maxDecompressedMessageBytes: null },
      completeness: { status: 'complete', missing: [] },
      rejectReason: offer.malformedReason,
    });
  } else if (!extCap.enabled || !offer) {
    extensionResults.push({
      name: 'permessage-deflate',
      verdict: VERDICT.OMITTED,
      responseHeader: null,
      rules: ['EX-1'],
      ruleTexts: [RULE.EX1],
      parameters: [],
      resourceBudget: null,
      budgetBinding: { bound: false, maxDecompressedMessageBytes: null },
      completeness: { status: 'complete', missing: [] },
    });
  } else {
    // PM-5：完整性闸门——两个方向的窗口位数都必须显式携带整数值
    const missing = extCap.completenessGate.requiredPresent.filter((key) => {
      const p = offer.params[key];
      return !p || !p.present || p.value === null;
    });

    if (missing.length > 0) {
      extensionResults.push({
        name: 'permessage-deflate',
        verdict: VERDICT.PENDING_CONFIRMATION,
        responseHeader: null,
        rules: ['PM-5'],
        ruleTexts: [RULE.PM5],
        parameters: [],
        resourceBudget: { ...extCap.uniformResourceBudget },
        budgetBinding: { bound: false, maxDecompressedMessageBytes: null },
        completeness: { status: 'incomplete', missing },
        pendingReason:
          `客户端声明缺少可确认的整数参数: ${missing.join(', ')}；` +
          'v2 禁止静默套用 RFC 缺省，需客户端补全后重新协商',
      });
    } else {
      const budget = extCap.uniformResourceBudget;
      const policyRange = extCap.policyWindowBitsRange;
      const parameters = [];
      const windows = {};

      // PM-1/PM-2：窗口位数 = min(客户端声明, 预算上限)，合法区间为 RFC ∩ 预算
      for (const [key, label] of [
        [PMD.SERVER_MAX_WINDOW_BITS, '服务端 LZ77 窗口位数'],
        [PMD.CLIENT_MAX_WINDOW_BITS, '客户端 LZ77 窗口位数'],
      ]) {
        const declared = offer.params[key].value;
        const applied = Math.min(declared, budget.maxWindowBits);
        windows[key] = applied;
        parameters.push({
          key,
          label,
          clientValue: declared,
          appliedValue: applied,
          source: applied < declared ? SOURCE.POLICY_CLAMP : SOURCE.CLIENT_OFFER,
          rfcRange: { ...RFC_RANGE },
          policyRange: { ...policyRange },
        });
      }

      // PM-3：双向 no_context_takeover 强制注入
      const flags = [PMD.SERVER_NO_CONTEXT_TAKEOVER, PMD.CLIENT_NO_CONTEXT_TAKEOVER];
      for (const key of flags) {
        parameters.push({
          key,
          label: key === PMD.SERVER_NO_CONTEXT_TAKEOVER ? '服务端禁止上下文接管' : '客户端禁止上下文接管',
          clientValue: offer.params[key]?.present ? true : null,
          appliedValue: true,
          source: SOURCE.POLICY_MANDATE,
          rfcRange: { ...RFC_RANGE },
          policyRange: { ...policyRange },
        });
      }

      extensionResults.push({
        name: 'permessage-deflate',
        verdict: VERDICT.ACCEPT,
        responseHeader: serializePmdResponse({ flags, windows }),
        rules: ['PM-1', 'PM-2', 'PM-3', 'PM-4', 'PM-6'],
        ruleTexts: ['PM-1', 'PM-2', 'PM-3', 'PM-4', 'PM-6'].map((r) => RULE[r]),
        parameters,
        resourceBudget: { ...budget },
        // PM-4：扩展启用即绑定解压消息预算
        budgetBinding: {
          bound: true,
          maxDecompressedMessageBytes: budget.maxDecompressedMessageBytes,
        },
        completeness: { status: 'complete', missing: [] },
      });
    }
  }

  const subprotocol = selectSubprotocol(parsed.subprotocols, capability);
  const responseHeaders = [
    ...(subprotocol.responseHeader ? [subprotocol.responseHeader] : []),
    ...extensionResults.filter((e) => e.responseHeader).map((e) => e.responseHeader),
  ];

  return {
    policyVersion: capability.policyVersion,
    route: 'uniform',
    subprotocol,
    extensions: extensionResults,
    responseHeaders,
  };
}
