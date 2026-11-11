/**
 * 离线协商回归测试（node:test，零依赖、无网络）。
 * 运行：npm test  或  node --test test/
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';

import { parseClientOffer } from '../src/parser.js';
import { negotiateV1 } from '../src/policy-v1.js';
import { negotiateV2 } from '../src/policy-v2.js';
import { auditDecision } from '../src/auditor.js';
import { CAPABILITY_V1, CAPABILITY_V2, RESOURCE_BUDGET } from '../src/capabilities.js';
import { PMD, SOURCE, VERDICT } from '../src/constants.js';
import { FIXTURES } from './fixtures.js';

const firstExt = (decision) => decision.extensions[0];
const paramOf = (ext, key) => ext.parameters.find((p) => p.key === key);

describe('解析器：离线头解析与 RFC 级校验', () => {
  test('解析子协议声明顺序与 permessage-deflate 整数参数', () => {
    const parsed = parseClientOffer({
      secWebSocketProtocol: 'wamp, mqtt',
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=12; client_max_window_bits=15',
    });
    assert.deepEqual(parsed.subprotocols, ['wamp', 'mqtt']);
    const offer = parsed.extensionOffers[0];
    assert.equal(offer.name, 'permessage-deflate');
    assert.equal(offer.params.server_max_window_bits.value, 12);
    assert.equal(offer.params.client_max_window_bits.value, 15);
    assert.equal(parsed.parseErrors.length, 0);
  });

  test('client_max_window_bits 裸标志可解析为无值（RFC 7692 通告语义）', () => {
    const parsed = parseClientOffer({
      secWebSocketExtensions: 'permessage-deflate; client_max_window_bits',
    });
    const offer = parsed.extensionOffers[0];
    assert.equal(offer.malformed, false);
    assert.equal(offer.params.client_max_window_bits.isFlag, true);
    assert.equal(offer.params.client_max_window_bits.value, null);
  });

  test('窗口位数超出 RFC [8,15] 标记为畸形', () => {
    const parsed = parseClientOffer({
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=16; client_max_window_bits=12',
    });
    assert.equal(parsed.extensionOffers[0].malformed, true);
    assert.match(parsed.parseErrors[0], /RFC 7692/);
  });

  test('重复参数与非整数标记为畸形，引号内逗号不切分', () => {
    const parsed = parseClientOffer({
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=abc; client_max_window_bits=12',
    });
    assert.equal(parsed.extensionOffers[0].malformed, true);
    assert.match(parsed.parseErrors[0], /不是整数/);

    const quoted = parseClientOffer({
      secWebSocketExtensions: 'x-vendor; name="a, b"; level=2',
    });
    assert.equal(quoted.extensionOffers.length, 1);
    assert.equal(quoted.extensionOffers[0].unknownParams.length, 2);
  });
});

describe('子协议选择 SP-1 ~ SP-3', () => {
  test('SP-1：按客户端声明顺序取第一个服务端支持项（非服务端偏好序）', () => {
    const parsed = parseClientOffer({ secWebSocketProtocol: 'soap, wamp, mqtt, chat.v2' });
    const d = negotiateV2(parsed, CAPABILITY_V2);
    assert.equal(d.subprotocol.selected, 'wamp');
    assert.equal(d.subprotocol.rule, 'SP-1');
    assert.deepEqual(d.subprotocol.candidateOrder, ['wamp', 'mqtt', 'chat.v2']);
  });

  test('SP-2：无公共子协议时不回应子协议头', () => {
    const parsed = parseClientOffer({ secWebSocketProtocol: 'soap, stomp' });
    const d = negotiateV2(parsed, CAPABILITY_V2);
    assert.equal(d.subprotocol.selected, null);
    assert.equal(d.subprotocol.rule, 'SP-2');
    assert.equal(d.subprotocol.responseHeader, null);
  });

  test('SP-3：客户端未声明子协议', () => {
    const parsed = parseClientOffer({ secWebSocketProtocol: '' });
    const d = negotiateV2(parsed, CAPABILITY_V2);
    assert.equal(d.subprotocol.rule, 'SP-3');
    assert.equal(d.subprotocol.selected, null);
  });
});

describe('可选扩展省略 EX-1 与畸形拒绝 EX-2', () => {
  test('EX-1：服务端未启用的扩展被省略，且不产生响应头', () => {
    const parsed = parseClientOffer({
      secWebSocketProtocol: 'mqtt',
      secWebSocketExtensions: 'x-supercompress; level=9',
    });
    const d = negotiateV2(parsed, CAPABILITY_V2);
    const ext = firstExt(d);
    assert.equal(ext.verdict, VERDICT.OMITTED);
    assert.deepEqual(ext.rules, ['EX-1']);
    assert.equal(ext.responseHeader, null);
  });

  test('EX-1：客户端未声明扩展时槽位省略', () => {
    const parsed = parseClientOffer({ secWebSocketProtocol: 'chat.v2' });
    const d = negotiateV2(parsed, CAPABILITY_V2);
    assert.equal(firstExt(d).verdict, VERDICT.OMITTED);
  });

  test('EX-2：畸形声明在两代策略下均被拒绝', () => {
    const parsed = parseClientOffer({
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=16; client_max_window_bits=12',
    });
    for (const d of [
      negotiateV1(parsed, CAPABILITY_V1, { route: 'configured' }),
      negotiateV1(parsed, CAPABILITY_V1, { route: 'default' }),
      negotiateV2(parsed, CAPABILITY_V2),
    ]) {
      const ext = firstExt(d);
      assert.equal(ext.verdict, VERDICT.REJECT);
      assert.deepEqual(ext.rules, ['EX-2']);
      assert.equal(ext.responseHeader, null);
    }
  });
});

describe('v1 配置分支：参数与资源限制正确绑定（基准正确实现）', () => {
  test('PM-1/PM-2：窗口 15 被钳制到预算 12，来源标记为 policy_clamp', () => {
    const parsed = parseClientOffer({
      secWebSocketProtocol: 'mqtt',
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=15; client_max_window_bits=15',
    });
    const d = negotiateV1(parsed, CAPABILITY_V1, { route: 'configured' });
    const ext = firstExt(d);
    assert.equal(ext.verdict, VERDICT.ACCEPT);
    assert.equal(paramOf(ext, PMD.SERVER_MAX_WINDOW_BITS).appliedValue, 12);
    assert.equal(paramOf(ext, PMD.CLIENT_MAX_WINDOW_BITS).appliedValue, 12);
    assert.equal(paramOf(ext, PMD.SERVER_MAX_WINDOW_BITS).source, SOURCE.POLICY_CLAMP);
    assert.deepEqual(paramOf(ext, PMD.SERVER_MAX_WINDOW_BITS).policyRange, { min: 8, max: 12 });
  });

  test('PM-3：双向 no_context_takeover 由服务端强制注入', () => {
    const parsed = parseClientOffer({
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=12; client_max_window_bits=12',
    });
    const ext = firstExt(negotiateV1(parsed, CAPABILITY_V1, { route: 'configured' }));
    assert.equal(paramOf(ext, PMD.SERVER_NO_CONTEXT_TAKEOVER).source, SOURCE.POLICY_MANDATE);
    assert.equal(paramOf(ext, PMD.CLIENT_NO_CONTEXT_TAKEOVER).appliedValue, true);
  });

  test('PM-4：决策携带资源预算快照并绑定 1 MiB 解压上限', () => {
    const parsed = parseClientOffer({
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=12; client_max_window_bits=12',
    });
    const ext = firstExt(negotiateV1(parsed, CAPABILITY_V1, { route: 'configured' }));
    assert.equal(ext.budgetBinding.bound, true);
    assert.equal(
      ext.budgetBinding.maxDecompressedMessageBytes,
      RESOURCE_BUDGET.maxDecompressedMessageBytes,
    );
    assert.equal(ext.resourceBudget.maxWindowBits, 12);
    const audit = auditDecision(negotiateV1(parsed, CAPABILITY_V1, { route: 'configured' }));
    assert.equal(audit.budgetSafe, true);
  });
});

describe('★ v1 默认分支：历史缺陷回归（PM-6 缺失）', () => {
  const parsed = parseClientOffer({
    secWebSocketProtocol: 'mqtt',
    secWebSocketExtensions:
      'permessage-deflate; server_max_window_bits=15; client_max_window_bits=15',
  });

  test('原样回显 15 位窗口，未做预算钳制', () => {
    const ext = firstExt(negotiateV1(parsed, CAPABILITY_V1, { route: 'default' }));
    assert.equal(paramOf(ext, PMD.SERVER_MAX_WINDOW_BITS).appliedValue, 15);
    assert.equal(paramOf(ext, PMD.SERVER_MAX_WINDOW_BITS).source, SOURCE.LEGACY_FALLBACK);
    // 缺陷特征：策略区间被放宽到 RFC 全区间，预算上限 12 不出现
    assert.deepEqual(paramOf(ext, PMD.SERVER_MAX_WINDOW_BITS).policyRange, { min: 8, max: 15 });
  });

  test('不强制注入 no_context_takeover，且不绑定解压预算', () => {
    const ext = firstExt(negotiateV1(parsed, CAPABILITY_V1, { route: 'default' }));
    assert.equal(paramOf(ext, PMD.SERVER_NO_CONTEXT_TAKEOVER), undefined);
    assert.equal(ext.budgetBinding.bound, false);
    assert.equal(ext.resourceBudget, null);
    assert.deepEqual(ext.rules, []); // PM-1..PM-4 规则在默认分支整体遗失
  });

  test('审计器复现防护失效：符合旧能力表但预算不安全', () => {
    const decision = negotiateV1(parsed, CAPABILITY_V1, { route: 'default' });
    const audit = auditDecision(decision);
    assert.equal(audit.policyConformant, true); // 忠实执行了缺限制的旧表
    assert.equal(audit.budgetSafe, false); // 资源预算被突破
    assert.equal(audit.isLegacyDefault, true);
    const failed = audit.checks.filter((c) => !c.pass);
    const effects = new Set(failed.map((c) => c.effect));
    assert.ok(effects.has('防护失效：窗口位数超出消息处理资源预算，压缩上下文内存超配'));
    assert.ok(effects.has('防护失效：未禁止上下文接管，跨消息状态不受预算约束'));
    assert.ok(effects.has('防护失效：未绑定解压消息字节预算，解压炸弹无上限'));
  });

  test('同一请求在配置分支与 v2 下均预算安全，证明缺陷仅限默认分支', () => {
    for (const decision of [
      negotiateV1(parsed, CAPABILITY_V1, { route: 'configured' }),
      negotiateV2(parsed, CAPABILITY_V2),
    ]) {
      assert.equal(auditDecision(decision).budgetSafe, true);
    }
  });
});

describe('v2 修正策略：统一预算与完整性闸门', () => {
  test('PM-6：默认路径同样钳制窗口并强制接管，审计通过', () => {
    const parsed = parseClientOffer({
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=15; client_max_window_bits=15',
    });
    const d = negotiateV2(parsed, CAPABILITY_V2);
    const ext = firstExt(d);
    assert.equal(paramOf(ext, PMD.SERVER_MAX_WINDOW_BITS).appliedValue, 12);
    assert.ok(ext.rules.includes('PM-6'));
    assert.equal(ext.budgetBinding.bound, true);
    assert.equal(auditDecision(d).budgetSafe, true);
  });

  test('PM-5：声明缺项（裸标志）单列 PENDING_CONFIRMATION，不产生响应头', () => {
    const parsed = parseClientOffer({
      secWebSocketProtocol: 'mqtt',
      secWebSocketExtensions: 'permessage-deflate; client_max_window_bits',
    });
    const d = negotiateV2(parsed, CAPABILITY_V2);
    const ext = firstExt(d);
    assert.equal(ext.verdict, VERDICT.PENDING_CONFIRMATION);
    assert.deepEqual(ext.rules, ['PM-5']);
    assert.deepEqual(ext.completeness.missing.sort(), [
      'client_max_window_bits',
      'server_max_window_bits',
    ]);
    assert.equal(ext.responseHeader, null);
  });

  test('PM-5：同一份不完整声明在 v1 被静默接受（对照，凸显闸门是 v2 新增）', () => {
    const parsed = parseClientOffer({
      secWebSocketExtensions: 'permessage-deflate; client_max_window_bits',
    });
    const d1 = negotiateV1(parsed, CAPABILITY_V1, { route: 'default' });
    assert.equal(firstExt(d1).verdict, VERDICT.ACCEPT);
    const d2 = negotiateV1(parsed, CAPABILITY_V1, { route: 'configured' });
    assert.equal(firstExt(d2).verdict, VERDICT.ACCEPT);
  });

  test('完整声明的响应头参数顺序稳定且全部在预算内', () => {
    const parsed = parseClientOffer({
      secWebSocketProtocol: 'mqtt',
      secWebSocketExtensions:
        'permessage-deflate; client_no_context_takeover; server_max_window_bits=15; ' +
        'client_max_window_bits=15',
    });
    const d = negotiateV2(parsed, CAPABILITY_V2);
    assert.deepEqual(d.responseHeaders, [
      'Sec-WebSocket-Protocol: mqtt',
      'Sec-WebSocket-Extensions: permessage-deflate; server_no_context_takeover; ' +
        'client_no_context_takeover; server_max_window_bits=12; client_max_window_bits=12',
    ]);
  });
});

describe('全夹具矩阵：预期与两代策略实际决策一致', () => {
  for (const fx of FIXTURES) {
    test(`${fx.id} ${fx.title}`, () => {
      const parsed = parseClientOffer(fx.rawHeaders);

      const dV1c = negotiateV1(parsed, CAPABILITY_V1, { route: 'configured' });
      const dV1d = negotiateV1(parsed, CAPABILITY_V1, { route: 'default' });
      const dV2 = negotiateV2(parsed, CAPABILITY_V2);

      assert.equal(firstExt(dV1c).verdict, fx.expected.v1Configured.extensionVerdict);
      assert.equal(firstExt(dV1d).verdict, fx.expected.v1Default.extensionVerdict);
      assert.equal(firstExt(dV2).verdict, fx.expected.v2.extensionVerdict);

      for (const [decision, col] of [
        [dV1c, 'v1Configured'],
        [dV1d, 'v1Default'],
        [dV2, 'v2'],
      ]) {
        assert.equal(decision.subprotocol.selected, fx.expected[col].subprotocol);
        if (fx.expected[col].appliedWindow !== undefined) {
          const w = paramOf(firstExt(decision), PMD.SERVER_MAX_WINDOW_BITS);
          assert.equal(w?.appliedValue, fx.expected[col].appliedWindow);
        }
        if (fx.expected[col].budgetSafe !== undefined) {
          assert.equal(auditDecision(decision).budgetSafe, fx.expected[col].budgetSafe);
        }
      }
    });
  }
});
