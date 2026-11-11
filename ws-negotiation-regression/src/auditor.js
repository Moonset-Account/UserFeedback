/**
 * 独立响应决策审计器。
 *
 * 关键设计：审计不依赖协商引擎的自我声明，而是拿“服务端消息处理的真实资源预算”
 * （RESUP: message-processing resource budget）重新核验每个决策，输出两层结论：
 *   - policyConformant：决策是否符合其声称使用的能力表条目（旧策略默认分支会“符合”
 *     一张本身就缺限制的表）；
 *   - budgetSafe：决策是否真正落在资源预算内（旧策略默认分支在此暴露防护失效）。
 * 两层结论分离，才能把“策略本身的缺陷”与“实现偏离策略”区分开。
 */

import { PMD, VERDICT } from './constants.js';
import { RESOURCE_BUDGET } from './capabilities.js';

const WINDOW_KEYS = [PMD.SERVER_MAX_WINDOW_BITS, PMD.CLIENT_MAX_WINDOW_BITS];
const TAKEOVER_KEYS = [PMD.SERVER_NO_CONTEXT_TAKEOVER, PMD.CLIENT_NO_CONTEXT_TAKEOVER];

export const AUDIT_EFFECT = Object.freeze({
  OK: '合规：参数与资源预算正确绑定',
  WINDOW_EXCEEDED: '防护失效：窗口位数超出消息处理资源预算，压缩上下文内存超配',
  TAKEOVER_MISSING: '防护失效：未禁止上下文接管，跨消息状态不受预算约束',
  BUDGET_UNBOUND: '防护失效：未绑定解压消息字节预算，解压炸弹无上限',
  OMIT: '兼容性降级：可选扩展按策略省略',
  REJECT: '协议拒绝：畸形声明被拦截',
  PENDING: '待确认：声明不完整，协商挂起',
  NO_SUBPROTOCOL: '兼容性降级：无公共子协议，回退裸 WebSocket',
});

function auditAcceptedExtension(ext) {
  const checks = [];

  // C1/C2：窗口位数与预算上限绑定
  for (const key of WINDOW_KEYS) {
    const param = ext.parameters.find((p) => p.key === key);
    const applied = param?.appliedValue;
    const pass = Number.isInteger(applied) && applied <= RESOURCE_BUDGET.maxWindowBits;
    checks.push({
      checkId: `C1-${key}`,
      item: param?.label ?? key,
      expected: `appliedValue <= ${RESOURCE_BUDGET.maxWindowBits}（预算上限）`,
      actual: applied === undefined ? '响应中缺失' : applied,
      pass,
      violatedRules: pass ? [] : ['PM-1', 'PM-2', 'PM-6'],
      effect: pass ? AUDIT_EFFECT.OK : AUDIT_EFFECT.WINDOW_EXCEEDED,
    });
  }

  // C3：双向 no_context_takeover 强制项
  for (const key of TAKEOVER_KEYS) {
    const param = ext.parameters.find((p) => p.key === key);
    const pass = param?.appliedValue === true;
    checks.push({
      checkId: `C3-${key}`,
      item: param?.label ?? key,
      expected: 'appliedValue === true（强制禁止上下文接管）',
      actual: param ? param.appliedValue : '响应中缺失',
      pass,
      violatedRules: pass ? [] : ['PM-3', 'PM-6'],
      effect: pass ? AUDIT_EFFECT.OK : AUDIT_EFFECT.TAKEOVER_MISSING,
    });
  }

  // C4：解压消息预算绑定
  const bound =
    ext.budgetBinding?.bound === true &&
    ext.budgetBinding?.maxDecompressedMessageBytes === RESOURCE_BUDGET.maxDecompressedMessageBytes;
  checks.push({
    checkId: 'C4-decompressed-budget',
    item: '解压消息字节预算绑定',
    expected: `绑定 maxDecompressedMessageBytes=${RESOURCE_BUDGET.maxDecompressedMessageBytes}`,
    actual: ext.budgetBinding?.bound
      ? ext.budgetBinding.maxDecompressedMessageBytes
      : '未绑定',
    pass: bound,
    violatedRules: bound ? [] : ['PM-4', 'PM-6'],
    effect: bound ? AUDIT_EFFECT.OK : AUDIT_EFFECT.BUDGET_UNBOUND,
  });

  // C5：决策必须携带资源预算快照
  const hasBudget = ext.resourceBudget !== null && ext.resourceBudget !== undefined;
  checks.push({
    checkId: 'C5-budget-snapshot',
    item: '资源预算快照',
    expected: '决策记录携带 resourceBudget',
    actual: hasBudget ? '已携带' : '缺失',
    pass: hasBudget,
    violatedRules: hasBudget ? [] : ['PM-4', 'PM-6'],
    effect: hasBudget ? AUDIT_EFFECT.OK : AUDIT_EFFECT.BUDGET_UNBOUND,
  });

  return checks;
}

/**
 * 审计单条协商决策。
 * @returns {{policyConformant: boolean, budgetSafe: boolean, checks: object[]}}
 */
export function auditDecision(decision) {
  const checks = [];

  // 子协议层审计
  const sp = decision.subprotocol;
  if (sp.rule === 'SP-2') {
    checks.push({
      checkId: 'SP-2',
      item: '子协议选择',
      expected: '存在公共子协议',
      actual: '无交集，未回应子协议头',
      pass: true,
      violatedRules: [],
      effect: AUDIT_EFFECT.NO_SUBPROTOCOL,
    });
  } else {
    checks.push({
      checkId: sp.rule,
      item: '子协议选择',
      expected: sp.rule === 'SP-3' ? '客户端未声明子协议' : `按声明顺序选中 ${sp.selected}`,
      actual: sp.selected ?? '未回应子协议头',
      pass: true,
      violatedRules: [],
      effect: AUDIT_EFFECT.OK,
    });
  }

  for (const ext of decision.extensions) {
    if (ext.verdict === VERDICT.ACCEPT) {
      checks.push(...auditAcceptedExtension(ext));
    } else if (ext.verdict === VERDICT.OMITTED) {
      checks.push({
        checkId: 'EX-1',
        item: `扩展 ${ext.name}`,
        expected: '能力表关闭或客户端未声明时省略',
        actual: '响应中不包含该扩展',
        pass: true,
        violatedRules: [],
        effect: AUDIT_EFFECT.OMIT,
      });
    } else if (ext.verdict === VERDICT.REJECT) {
      checks.push({
        checkId: 'EX-2',
        item: `扩展 ${ext.name}`,
        expected: '畸形声明必须拒绝',
        actual: `已拒绝：${ext.rejectReason}`,
        pass: true,
        violatedRules: [],
        effect: AUDIT_EFFECT.REJECT,
      });
    } else if (ext.verdict === VERDICT.PENDING_CONFIRMATION) {
      checks.push({
        checkId: 'PM-5',
        item: `扩展 ${ext.name}`,
        expected: '声明完整时才允许协商',
        actual: `待确认，缺项：${ext.completeness.missing.join(', ')}`,
        pass: true,
        violatedRules: [],
        effect: AUDIT_EFFECT.PENDING,
      });
    }
  }

  const isLegacyDefault =
    decision.policyVersion === 'v1-legacy' &&
    decision.route === 'default' &&
    decision.extensions.some((e) => e.verdict === VERDICT.ACCEPT && e.rules.length === 0);

  // 预算安全：只看 C 系列资源检查；没有启用扩展（省略/拒绝/待确认）时天然安全
  const budgetSafe = checks
    .filter((c) => c.checkId.startsWith('C'))
    .every((c) => c.pass);

  // 策略符合性：v1 默认分支忠实执行了一张“本身就缺限制”的能力表，
  // 因此 policyConformant=true 而 budgetSafe=false —— 双轨分离正好暴露策略级缺陷；
  // 其余路径的策略要求与资源预算一致，两个结论同真同假。
  const policyConformant = isLegacyDefault ? true : budgetSafe;

  return {
    policyConformant,
    budgetSafe,
    isLegacyDefault,
    checks,
  };
}
