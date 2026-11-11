/**
 * 旧策略 v1 协商引擎（含历史缺陷，用于回归复现）。
 *
 * 缺陷定位：permessage-deflate 的默认分支（route='default'）走 legacy 回退——
 *   1) 原样回显客户端声明的窗口位数，不与 maxWindowBits=12 的预算做钳制（违反 PM-1）；
 *   2) 不强制注入 no_context_takeover（违反 PM-3）；
 *   3) 不绑定 maxDecompressedMessageBytes 解压预算（违反 PM-4）；
 *   4) 实际生效的窗口区间退回 RFC 的 [8,15]，覆盖服务端预算（违反 PM-2/PM-6）。
 * 配置分支（route='configured'）逻辑正确，作为同版对照证明缺陷仅存在于默认分支。
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

function findOffer(parsed, name) {
  return parsed.extensionOffers.find((o) => o.name === name) ?? null;
}

/** 配置分支：预算约束完整的正确实现（v2 的行为与此一致） */
function negotiateConfiguredPmd(offer, extCap) {
  const branch = extCap.branches.configured;
  const budget = branch.resourceBudget;
  const policyRange = branch.policyWindowBitsRange;
  const rules = [];
  const parameters = [];

  const resolveWindow = (key, label) => {
    const declared = offer.params[key];
    if (declared && declared.present && declared.value !== null) {
      const clamped = Math.min(declared.value, budget.maxWindowBits);
      parameters.push({
        key,
        label,
        clientValue: declared.value,
        appliedValue: clamped,
        source: clamped < declared.value ? SOURCE.POLICY_CLAMP : SOURCE.CLIENT_OFFER,
        rfcRange: { ...RFC_RANGE },
        policyRange: { ...policyRange },
      });
      return clamped;
    }
    // 缺省或裸标志：策略以预算上限作为默认窗口
    parameters.push({
      key,
      label,
      clientValue: declared?.present && declared.value === null ? 'bare-flag' : null,
      appliedValue: budget.maxWindowBits,
      source: SOURCE.POLICY_DEFAULT,
      rfcRange: { ...RFC_RANGE },
      policyRange: { ...policyRange },
    });
    return budget.maxWindowBits;
  };

  const serverBits = resolveWindow(PMD.SERVER_MAX_WINDOW_BITS, '服务端 LZ77 窗口位数');
  const clientBits = resolveWindow(PMD.CLIENT_MAX_WINDOW_BITS, '客户端 LZ77 窗口位数');
  rules.push('PM-1', 'PM-2');

  // PM-3：两个方向的 no_context_takeover 均由服务端强制注入
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
  rules.push('PM-3', 'PM-4');

  return {
    name: 'permessage-deflate',
    verdict: VERDICT.ACCEPT,
    responseHeader: serializePmdResponse({
      flags,
      windows: {
        [PMD.SERVER_MAX_WINDOW_BITS]: serverBits,
        [PMD.CLIENT_MAX_WINDOW_BITS]: clientBits,
      },
    }),
    rules,
    ruleTexts: rules.map((r) => RULE[r]),
    parameters,
    resourceBudget: { ...budget },
    budgetBinding: { bound: true, maxDecompressedMessageBytes: budget.maxDecompressedMessageBytes },
    completeness: { status: 'complete', missing: [] },
  };
}

/** 默认分支：遗留回退（缺陷代码路径） */
function negotiateDefaultPmd(offer) {
  const rules = [];
  const parameters = [];
  const flags = [];
  const windows = {};

  // 遗留行为：窗口位数原样回显，仅受 RFC 区间约束，无预算钳制
  for (const [key, label] of [
    [PMD.SERVER_MAX_WINDOW_BITS, '服务端 LZ77 窗口位数'],
    [PMD.CLIENT_MAX_WINDOW_BITS, '客户端 LZ77 窗口位数'],
  ]) {
    const declared = offer.params[key];
    if (declared?.present && declared.value !== null) {
      windows[key] = declared.value;
      parameters.push({
        key,
        label,
        clientValue: declared.value,
        appliedValue: declared.value,
        source: SOURCE.LEGACY_FALLBACK,
        rfcRange: { ...RFC_RANGE },
        // 缺陷特征：策略区间被放宽为整个 RFC 区间，预算上限 12 不生效
        policyRange: { ...RFC_RANGE },
      });
    } else if (declared?.present && declared.value === null) {
      // 裸标志：遗留代码不回应该参数，等价于静默套用 RFC 缺省（15）
      parameters.push({
        key,
        label,
        clientValue: 'bare-flag',
        appliedValue: null,
        source: SOURCE.RFC_DEFAULT,
        rfcRange: { ...RFC_RANGE },
        policyRange: { ...RFC_RANGE },
      });
    }
  }

  // 遗留行为：只回显客户端自己声明的接管标志，服务端不强制注入
  for (const key of [PMD.SERVER_NO_CONTEXT_TAKEOVER, PMD.CLIENT_NO_CONTEXT_TAKEOVER]) {
    if (offer.params[key]?.present) {
      flags.push(key);
      parameters.push({
        key,
        label: key === PMD.SERVER_NO_CONTEXT_TAKEOVER ? '服务端禁止上下文接管' : '客户端禁止上下文接管',
        clientValue: true,
        appliedValue: true,
        source: SOURCE.LEGACY_FALLBACK,
        rfcRange: { ...RFC_RANGE },
        policyRange: { ...RFC_RANGE },
      });
    }
  }

  return {
    name: 'permessage-deflate',
    verdict: VERDICT.ACCEPT,
    responseHeader: serializePmdResponse({ flags, windows }),
    // 注意：无 PM-1..PM-4 规则——默认分支遗失的正是这些限制（PM-6 缺失）
    rules,
    ruleTexts: [],
    parameters,
    // 缺陷特征：无资源预算、无解压预算绑定
    resourceBudget: null,
    budgetBinding: { bound: false, maxDecompressedMessageBytes: null },
    completeness: { status: 'complete', missing: [] },
  };
}

/**
 * v1 协商入口。
 * @param {object} parsed parser.js 的解析结果
 * @param {object} capability CAPABILITY_V1
 * @param {{route: 'configured'|'default'}} options
 */
export function negotiateV1(parsed, capability, { route } = {}) {
  const extCap = capability.extensions['permessage-deflate'];
  const offer = findOffer(parsed, 'permessage-deflate');
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
    // EX-1：可选扩展按能力表开关/客户端未声明而省略
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
  } else if (route === 'configured') {
    extensionResults.push(negotiateConfiguredPmd(offer, extCap));
  } else {
    extensionResults.push(negotiateDefaultPmd(offer));
  }

  const subprotocol = selectSubprotocol(parsed.subprotocols, capability);
  const responseHeaders = [
    ...(subprotocol.responseHeader ? [subprotocol.responseHeader] : []),
    ...extensionResults.filter((e) => e.responseHeader).map((e) => e.responseHeader),
  ];

  return {
    policyVersion: capability.policyVersion,
    route,
    subprotocol,
    extensions: extensionResults,
    responseHeaders,
  };
}
