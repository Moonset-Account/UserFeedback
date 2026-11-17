// Synthetic, locally generated CSP3 scenarios. Every check here is a real
// assertion evaluated against lib/csp3.mjs output (no mocks of the library).

import { evaluate, sha256Base64 } from '../lib/csp3.mjs';

const ORIGIN = 'https://app.test';

function findDecision(result, scriptId, policyId) {
  const script = result.scripts.find((s) => s.scriptId === scriptId);
  return script.decisions.find((d) => d.policyId === policyId);
}

function findReport(result, scriptId, policyId) {
  const script = result.scripts.find((s) => s.scriptId === scriptId);
  return script.reports.find((r) => r.policyId === policyId);
}

function findPolicy(result, policyId) {
  return result.policies.find((p) => p.id === policyId);
}

// ---------------------------------------------------------------------------
// C01: fallback chain, duplicate directives, self/none/HTTPS hosts,
//      and explicit reporting of unsupported syntax.
// ---------------------------------------------------------------------------

const caseC01 = {
  id: 'C01',
  title: '指令回退 / 重复指令 / self·none·HTTPS 主机源 / 不支持语法',
  run(assert) {
    const policies = [
      {
        id: 'p1-enforce',
        mode: 'enforce',
        text: "default-src 'none'; script-src https://cdn.test; script-src 'self'",
      },
      {
        id: 'p2-enforce',
        mode: 'enforce',
        text: "default-src 'self'",
      },
      {
        id: 'p3-enforce',
        mode: 'enforce',
        text: "script-src 'unsafe-inline' data: *.wild.test 'sha384-abc' https:///; unknown-directive https://x.test",
      },
      {
        id: 'p4-none',
        mode: 'enforce',
        text: "script-src 'none'",
      },
    ];

    const scripts = [
      { id: 's1-cdn', kind: 'external', url: 'https://cdn.test/a.js', parserInserted: true },
      { id: 's2-self', kind: 'external', url: 'https://app.test/b.js', parserInserted: true },
      { id: 's3-wild', kind: 'external', url: 'https://sub.wild.test/c.js', parserInserted: true },
      { id: 's4-inline', kind: 'inline', body: 'console.log(1)', parserInserted: true },
      { id: 's5-evil', kind: 'external', url: 'https://other.test/d.js', parserInserted: true },
    ];

    const result = evaluate({ origin: ORIGIN, policies, scripts });

    // Effective directive selection: explicit script-src beats default-src;
    // fallback reaches default-src only when neither script directive exists.
    assert(findDecision(result, 's1-cdn', 'p1-enforce').directive === 'script-src',
      'C01-01 p1 使用 script-src 而非回退到 default-src',
      { expected: 'script-src', observed: findDecision(result, 's1-cdn', 'p1-enforce').directive });
    assert(findDecision(result, 's1-cdn', 'p2-enforce').directive === 'default-src',
      'C01-02 p2 无 script-src 时回退到 default-src',
      { expected: 'default-src', observed: findDecision(result, 's1-cdn', 'p2-enforce').directive });

    // Duplicate directive: first occurrence wins, second is recorded.
    const p1 = findPolicy(result, 'p1-enforce');
    assert(p1.duplicates.length === 1 && p1.duplicates[0].directive === 'script-src',
      'C01-03 重复 script-src 被记录且按 CSP3 忽略后者',
      { expected: 1, observed: p1.duplicates.length });
    assert(findDecision(result, 's2-self', 'p1-enforce').allowed === false,
      'C01-04 被忽略的第二份 script-src(\'self\') 不放行同源脚本',
      { expected: false, observed: findDecision(result, 's2-self', 'p1-enforce').allowed });

    // HTTPS host-source matching.
    assert(findDecision(result, 's1-cdn', 'p1-enforce').allowed === true
      && findDecision(result, 's1-cdn', 'p1-enforce').basis[0] === 'host:https://cdn.test',
      'C01-05 https 主机源精确放行 cdn.test',
      { expected: 'host:https://cdn.test', observed: findDecision(result, 's1-cdn', 'p1-enforce').basis });
    assert(findDecision(result, 's3-wild', 'p3-enforce').allowed === true
      && findDecision(result, 's3-wild', 'p3-enforce').basis[0] === 'host:*.wild.test',
      'C01-06 通配主机源 *.wild.test 仅匹配子域',
      { expected: 'host:*.wild.test', observed: findDecision(result, 's3-wild', 'p3-enforce').basis });
    assert(findDecision(result, 's5-evil', 'p3-enforce').allowed === false,
      'C01-07 不匹配的外部主机被拦截',
      { expected: false, observed: findDecision(result, 's5-evil', 'p3-enforce').allowed });

    // 'self' and 'none'.
    assert(findDecision(result, 's2-self', 'p2-enforce').allowed === true
      && findDecision(result, 's2-self', 'p2-enforce').basis[0] === 'host:self',
      'C01-08 default-src \'self\' 放行文档同源脚本',
      { expected: 'host:self', observed: findDecision(result, 's2-self', 'p2-enforce').basis });
    assert(findDecision(result, 's1-cdn', 'p4-none').allowed === false
      && findDecision(result, 's1-cdn', 'p4-none').reason === "blocked:source-list-is-'none'",
      'C01-09 仅含 \'none\' 的策略阻断全部脚本',
      { expected: "blocked:source-list-is-'none'", observed: findDecision(result, 's1-cdn', 'p4-none').reason });

    // Inline scripts cannot be authorised by host sources.
    assert(findDecision(result, 's4-inline', 'p1-enforce').reason
      === 'blocked:inline-script-without-nonce-or-sha256',
      'C01-10 主机源策略下无 nonce/hash 的内联脚本被拦截',
      { expected: 'blocked:inline-script-without-nonce-or-sha256',
        observed: findDecision(result, 's4-inline', 'p1-enforce').reason });

    // Unsupported syntax is reported explicitly, never silently accepted.
    const p3 = findPolicy(result, 'p3-enforce');
    const reasons = p3.unsupported.map((u) => u.reason).sort();
    for (const [tag, want] of [
      ['C01-11a', 'unsupported-keyword'],        // 'unsafe-inline'
      ['C01-11b', 'unsupported-scheme'],         // data:
      ['C01-11c', 'unsupported-hash-algorithm'], // 'sha384-...'
      ['C01-11d', 'malformed-host'],             // https:///
      ['C01-11e', 'unsupported-directive'],      // unknown-directive
    ]) {
      assert(reasons.includes(want), `${tag} 不支持语法明确上报: ${want}`,
        { expected: want, observed: reasons });
    }
    assert(findDecision(result, 's3-wild', 'p3-enforce').directiveText
      === p3.effectiveDirectiveText,
      'C01-12 不支持的 token 不影响其余受支持 token 的解析与匹配',
      { expected: p3.effectiveDirectiveText,
        observed: findDecision(result, 's3-wild', 'p3-enforce').directiveText });

    // Intersection: every enforcing policy must allow.
    assert(result.scripts.find((s) => s.scriptId === 's1-cdn').executes === false,
      'C01-13 单策略放行不等于可执行：p2/p3/p4 仍阻断 s1',
      { expected: false, observed: result.scripts.find((s) => s.scriptId === 's1-cdn').executes });

    return {
      seed: { origin: ORIGIN, policyCount: policies.length, scriptCount: scripts.length },
      sample: { policies, scripts },
      summary: result,
    };
  },
};

// ---------------------------------------------------------------------------
// C02: nonce, SHA-256 hash and strict-dynamic parser/dynamic decisions.
// ---------------------------------------------------------------------------

const caseC02 = {
  id: 'C02',
  title: 'nonce / SHA-256 / strict-dynamic 的解析器与动态脚本判定',
  run(assert) {
    const inlineBody = 'var a=1;';
    const inlineHash = sha256Base64(inlineBody);

    const policies = [
      { id: 'p1-dynamic', mode: 'enforce', text: "script-src 'strict-dynamic' 'nonce-N1' https://cdn.test" },
      { id: 'p2-hash', mode: 'enforce', text: `script-src 'sha256-${inlineHash}'` },
    ];

    const scripts = [
      { id: 's1-loader', kind: 'external', url: 'https://cdn.test/loader.js', nonce: 'N1', parserInserted: true },
      { id: 's2-inline-hash', kind: 'inline', body: inlineBody, parserInserted: true },
      { id: 's3-dynamic-child', kind: 'external', url: 'https://cdn.test/child.js', parserInserted: false, creator: 's1-loader' },
      { id: 's4-parser-child', kind: 'external', url: 'https://cdn.test/parsed.js', parserInserted: true, creator: 's1-loader' },
      { id: 's5-both', kind: 'inline', body: inlineBody, nonce: 'N1', parserInserted: true },
      { id: 's6-bad-nonce', kind: 'inline', body: 'other', nonce: 'WRONG', parserInserted: true },
    ];

    const result = evaluate({ origin: ORIGIN, policies, scripts });

    // Nonce takes precedence and beats the strict-dynamic host allowlist.
    assert(findDecision(result, 's1-loader', 'p1-dynamic').allowed === true
      && findDecision(result, 's1-loader', 'p1-dynamic').basis[0] === 'nonce',
      'C02-01 正确 nonce 在 strict-dynamic 策略下放行解析器外部脚本',
      { expected: 'nonce', observed: findDecision(result, 's1-loader', 'p1-dynamic').basis });
    assert(findDecision(result, 's6-bad-nonce', 'p1-dynamic').allowed === false,
      'C02-02 错误 nonce 不命中，strict-dynamic 下解析器脚本被拦截',
      { expected: false, observed: findDecision(result, 's6-bad-nonce', 'p1-dynamic').allowed });
    assert(findDecision(result, 's6-bad-nonce', 'p1-dynamic').reason
      === 'blocked:parser-inserted-and-strict-dynamic-ignores-host-sources',
      'C02-03 strict-dynamic 明确忽略主机源，解析器脚本无 nonce/hash 即阻断',
      { expected: 'blocked:parser-inserted-and-strict-dynamic-ignores-host-sources',
        observed: findDecision(result, 's6-bad-nonce', 'p1-dynamic').reason });

    // SHA-256 applies to inline bodies only.
    assert(findDecision(result, 's2-inline-hash', 'p2-hash').allowed === true
      && findDecision(result, 's2-inline-hash', 'p2-hash').basis[0] === 'sha256',
      'C02-04 内联正文 SHA-256 命中放行',
      { expected: 'sha256', observed: findDecision(result, 's2-inline-hash', 'p2-hash').basis });
    assert(findDecision(result, 's1-loader', 'p2-hash').allowed === false
      && findDecision(result, 's1-loader', 'p2-hash').reason === 'blocked:no-matching-host-source',
      'C02-05 hash 不适用于外部脚本，hash-only 策略阻断外部加载',
      { expected: 'blocked:no-matching-host-source',
        observed: findDecision(result, 's1-loader', 'p2-hash').reason });

    // strict-dynamic propagation: non-parser child of a trusted creator.
    assert(findDecision(result, 's3-dynamic-child', 'p1-dynamic').allowed === true
      && findDecision(result, 's3-dynamic-child', 'p1-dynamic').basis[0]
        === 'strict-dynamic:creator:s1-loader',
      'C02-06 受信脚本创建的非解析器子脚本经 strict-dynamic 传播放行',
      { expected: 'strict-dynamic:creator:s1-loader',
        observed: findDecision(result, 's3-dynamic-child', 'p1-dynamic').basis });
    assert(findDecision(result, 's4-parser-child', 'p1-dynamic').allowed === false,
      'C02-07 解析器插入的子脚本不能继承 strict-dynamic 信任',
      { expected: false, observed: findDecision(result, 's4-parser-child', 'p1-dynamic').allowed });

    // The hash-only policy has no strict-dynamic, so creator trust there is
    // never propagated.
    assert(findDecision(result, 's3-dynamic-child', 'p2-hash').allowed === false,
      'C02-08 无 strict-dynamic 时创建者信任不传播，外部子脚本仍被拦截',
      { expected: false, observed: findDecision(result, 's3-dynamic-child', 'p2-hash').allowed });

    // Both policies pass -> executable.
    assert(result.scripts.find((s) => s.scriptId === 's5-both').executes === true,
      'C02-09 同一脚本在两份策略分别命中 nonce 与 sha256 时方可执行',
      { expected: true, observed: result.scripts.find((s) => s.scriptId === 's5-both').executes });
    assert(result.scripts.find((s) => s.scriptId === 's1-loader').executes === false,
      'C02-10 仅 p1 放行的 loader 因 p2 阻断而不可执行',
      { expected: false, observed: result.scripts.find((s) => s.scriptId === 's1-loader').executes });

    return {
      seed: { origin: ORIGIN, inlineHashSha256: inlineHash, nonce: 'N1' },
      sample: { policies, scripts },
      summary: result,
    };
  },
};

// ---------------------------------------------------------------------------
// C03: per-policy creator trust, intersected; a dynamic child passes only
//      some policies, and trust never crosses policy boundaries.
// ---------------------------------------------------------------------------

const caseC03 = {
  id: 'C03',
  title: '逐策略信任传播后求交：动态子脚本仅在部分策略通过',
  run(assert) {
    const policies = [
      { id: 'p1-strict', mode: 'enforce', text: "script-src 'strict-dynamic' 'nonce-N1'" },
      { id: 'p2-host', mode: 'enforce', text: 'script-src https://cdn.test' },
    ];

    const scripts = [
      { id: 'loader', kind: 'external', url: 'https://cdn.test/loader.js', nonce: 'N1', parserInserted: true },
      { id: 'child', kind: 'external', url: 'https://evil.example/child.js', parserInserted: false, creator: 'loader' },
      { id: 'grandchild', kind: 'external', url: 'https://evil.example/grand.js', parserInserted: false, creator: 'child' },
      { id: 'inline-child', kind: 'inline', body: 'dyn()', parserInserted: false, creator: 'loader' },
      // Trusted under p2 only (host allowlist), never under p1 (no nonce, parser-inserted).
      { id: 'host-only-loader', kind: 'external', url: 'https://cdn.test/l2.js', parserInserted: true },
      { id: 'cross-policy-child', kind: 'external', url: 'https://evil.example/x.js', parserInserted: false, creator: 'host-only-loader' },
    ];

    const result = evaluate({ origin: ORIGIN, policies, scripts });

    // Loader is trusted under both policies, but for different reasons.
    assert(findDecision(result, 'loader', 'p1-strict').basis[0] === 'nonce'
      && findDecision(result, 'loader', 'p2-host').basis[0] === 'host:https://cdn.test',
      'C03-01 同一 loader 在两策略下分别以 nonce 与主机源获信',
      { expected: ['nonce', 'host:https://cdn.test'],
        observed: [findDecision(result, 'loader', 'p1-strict').basis[0],
          findDecision(result, 'loader', 'p2-host').basis[0]] });

    // Dynamic child: strict-dynamic passes on p1; the host-only p2 blocks.
    assert(findDecision(result, 'child', 'p1-strict').allowed === true
      && findDecision(result, 'child', 'p1-strict').basis[0] === 'strict-dynamic:creator:loader',
      'C03-02 p1 经 strict-dynamic 放行跨域动态子脚本',
      { expected: 'strict-dynamic:creator:loader',
        observed: findDecision(result, 'child', 'p1-strict').basis });
    assert(findDecision(result, 'child', 'p2-host').allowed === false
      && findDecision(result, 'child', 'p2-host').reason === 'blocked:no-matching-host-source',
      'C03-03 p2 无 strict-dynamic，主机限制拦截同一动态子脚本',
      { expected: 'blocked:no-matching-host-source',
        observed: findDecision(result, 'child', 'p2-host').reason });
    assert(result.scripts.find((s) => s.scriptId === 'child').executes === false,
      'C03-04 求交结果：仅部分策略通过的子脚本最终不可执行',
      { expected: false, observed: result.scripts.find((s) => s.scriptId === 'child').executes });

    // Trust chain continues only inside the policy that trusted the parent.
    assert(findDecision(result, 'grandchild', 'p1-strict').allowed === true
      && findDecision(result, 'grandchild', 'p1-strict').basis[0]
        === 'strict-dynamic:creator:child',
      'C03-05 p1 沿 child 继续传播信任到孙脚本',
      { expected: 'strict-dynamic:creator:child',
        observed: findDecision(result, 'grandchild', 'p1-strict').basis });
    assert(findDecision(result, 'grandchild', 'p2-host').allowed === false,
      'C03-06 p2 中 child 未获信，孙脚本同样被拦截',
      { expected: false, observed: findDecision(result, 'grandchild', 'p2-host').allowed });

    // Dynamic inline child follows the same per-policy split.
    assert(findDecision(result, 'inline-child', 'p1-strict').allowed === true
      && findDecision(result, 'inline-child', 'p2-host').allowed === false,
      'C03-07 动态内联子脚本同样只在 strict-dynamic 策略下通过',
      { expected: { p1: true, p2: false },
        observed: {
          p1: findDecision(result, 'inline-child', 'p1-strict').allowed,
          p2: findDecision(result, 'inline-child', 'p2-host').allowed,
        } });

    // Trust does not cross policy boundaries.
    assert(findDecision(result, 'host-only-loader', 'p2-host').allowed === true
      && findDecision(result, 'host-only-loader', 'p1-strict').allowed === false,
      'C03-08 host-only-loader 仅在 p2 获信，p1 拒绝',
      { expected: { p1: false, p2: true },
        observed: {
          p1: findDecision(result, 'host-only-loader', 'p1-strict').allowed,
          p2: findDecision(result, 'host-only-loader', 'p2-host').allowed,
        } });
    assert(findDecision(result, 'cross-policy-child', 'p1-strict').allowed === false
      && findDecision(result, 'cross-policy-child', 'p1-strict').reason
        === 'blocked:creator-not-trusted-under-this-policy',
      'C03-09 p2 下获信的创建者不能替 p1 授信：信任不跨策略',
      { expected: 'blocked:creator-not-trusted-under-this-policy',
        observed: findDecision(result, 'cross-policy-child', 'p1-strict').reason });

    // Violation records name the blocking policy per script.
    const childReports = result.scripts.find((s) => s.scriptId === 'child').reports.map((r) => r.policyId);
    assert(childReports.length === 1 && childReports[0] === 'p2-host',
      'C03-10 违规信息逐策略生成，仅记录实际阻断的 p2-host',
      { expected: ['p2-host'], observed: childReports });

    return {
      seed: { origin: ORIGIN, policyCount: policies.length, scriptCount: scripts.length, nonce: 'N1' },
      sample: { policies, scripts },
      summary: result,
    };
  },
};

// ---------------------------------------------------------------------------
// C04: Report-Only never blocks; directive/matching basis and final status
//      are exposed for every policy.
// ---------------------------------------------------------------------------

const caseC04 = {
  id: 'C04',
  title: 'Report-Only 不阻断 + 指令/匹配依据/最终执行状态可追溯',
  run(assert) {
    const policies = [
      { id: 'gw-enforce', mode: 'enforce', text: 'script-src https://cdn.test' },
      { id: 'app-report', mode: 'report-only', text: "script-src 'strict-dynamic' 'nonce-N2'" },
      { id: 'ro-fallback', mode: 'report-only', text: "default-src 'none'" },
    ];

    const scripts = [
      { id: 's1-host-ok', kind: 'external', url: 'https://cdn.test/a.js', parserInserted: true },
      { id: 's2-blocked', kind: 'external', url: 'https://evil.example/b.js', parserInserted: true },
      { id: 's3-nonce-ok', kind: 'external', url: 'https://cdn.test/c.js', nonce: 'N2', parserInserted: true },
      { id: 's4-dynamic', kind: 'external', url: 'https://evil.example/d.js', parserInserted: false, creator: 's3-nonce-ok' },
    ];

    const result = evaluate({ origin: ORIGIN, policies, scripts });

    // Report-Only violations do not change the executable status.
    const s1 = result.scripts.find((s) => s.scriptId === 's1-host-ok');
    assert(s1.executes === true,
      'C04-01 Report-Only 阻断不影响执行：enforce 放行即执行',
      { expected: true, observed: s1.executes });
    assert(findDecision(result, 's1-host-ok', 'app-report').allowed === false
      && findReport(result, 's1-host-ok', 'app-report').disposition === 'report-only',
      'C04-02 Report-Only 策略仍生成违规记录且标记 disposition',
      { expected: 'report-only',
        observed: findReport(result, 's1-host-ok', 'app-report').disposition });
    assert(findDecision(result, 's1-host-ok', 'ro-fallback').directive === 'default-src'
      && findDecision(result, 's1-host-ok', 'ro-fallback').allowed === false,
      'C04-03 Report-Only 同样经历指令回退并记录 \'none\' 阻断依据',
      { expected: { directive: 'default-src', allowed: false },
        observed: {
          directive: findDecision(result, 's1-host-ok', 'ro-fallback').directive,
          allowed: findDecision(result, 's1-host-ok', 'ro-fallback').allowed,
        } });

    // Enforcing block still wins even when a Report-Only policy would allow.
    const s4 = result.scripts.find((s) => s.scriptId === 's4-dynamic');
    assert(findDecision(result, 's4-dynamic', 'app-report').allowed === true
      && findDecision(result, 's4-dynamic', 'app-report').basis[0]
        === 'strict-dynamic:creator:s3-nonce-ok',
      'C04-04 Report-Only 下 strict-dynamic 正常传播并放行动态子脚本',
      { expected: 'strict-dynamic:creator:s3-nonce-ok',
        observed: findDecision(result, 's4-dynamic', 'app-report').basis });
    assert(findDecision(result, 's4-dynamic', 'gw-enforce').allowed === false
      && s4.executes === false,
      'C04-05 enforce 主机策略阻断优先：Report-Only 放行也不能执行',
      { expected: { enforceAllowed: false, executes: false },
        observed: {
          enforceAllowed: findDecision(result, 's4-dynamic', 'gw-enforce').allowed,
          executes: s4.executes,
        } });

    // Fully blocked script reports from every policy that blocks it.
    const s2Reports = result.scripts.find((s) => s.scriptId === 's2-blocked').reports
      .map((r) => r.policyId).sort();
    assert(JSON.stringify(s2Reports) === JSON.stringify(['app-report', 'gw-enforce', 'ro-fallback']),
      'C04-06 被拦脚本逐策略给出违规记录（含 enforce 与 report-only）',
      { expected: ['app-report', 'gw-enforce', 'ro-fallback'], observed: s2Reports });
    assert(result.scripts.find((s) => s.scriptId === 's2-blocked').executes === false,
      'C04-07 enforce 阻断的脚本最终不可执行',
      { expected: false, observed: result.scripts.find((s) => s.scriptId === 's2-blocked').executes });

    // Every decision carries directive, directive text and matching basis/reason.
    const s3Decision = findDecision(result, 's3-nonce-ok', 'app-report');
    assert(s3Decision.directive === 'script-src'
      && typeof s3Decision.directiveText === 'string'
      && s3Decision.basis[0] === 'nonce',
      'C04-08 每个判定可追溯到使用的指令、原始指令文本与匹配依据',
      { expected: { directive: 'script-src', basis: 'nonce' },
        observed: { directive: s3Decision.directive, basis: s3Decision.basis } });
    const gw = findPolicy(result, 'gw-enforce');
    assert(gw.mode === 'enforce' && gw.effectiveDirective === 'script-src'
      && gw.effectiveDirectiveText === 'script-src https://cdn.test',
      'C04-09 策略摘要输出模式、生效指令与指令文本',
      { expected: 'script-src https://cdn.test', observed: gw.effectiveDirectiveText });
    assert(findDecision(result, 's1-host-ok', 'gw-enforce').basis[0] === 'host:https://cdn.test',
      'C04-10 网关主机策略的匹配依据可直接定位到具体来源表达式',
      { expected: 'host:https://cdn.test',
        observed: findDecision(result, 's1-host-ok', 'gw-enforce').basis });

    return {
      seed: { origin: ORIGIN, policyCount: policies.length, scriptCount: scripts.length, nonce: 'N2' },
      sample: { policies, scripts },
      summary: result,
    };
  },
};

export const CASES = [caseC01, caseC02, caseC03, caseC04];
