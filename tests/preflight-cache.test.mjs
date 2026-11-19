import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  PreflightCache,
  DEFAULT_MAX_AGE_SECONDS,
  WILDCARD,
} from '../src/preflight-cache.js';

// 手动时钟：完全控制“现在”，用于确定性地验证过期与不续期语义。
function manualClock(start = 1_000_000) {
  let t = start;
  return {
    now: () => t,
    advance(ms) { t += ms; },
    set(ms) { t = ms; },
  };
}

const SITE_A = 'https://site-a.example';
const SITE_B = 'https://site-b.example';
const ORIGIN = 'https://app.example';
const API = 'https://api.example/v1/data';
const API_OTHER = 'https://api.example/v1/other';

function storeFull(cache, overrides = {}) {
  return cache.store({
    partition: SITE_A,
    origin: ORIGIN,
    url: API,
    maxAge: 10,
    allowMethods: ['PUT'],
    allowHeaders: ['x-custom', 'x-other'],
    ...overrides,
  });
}

describe('可注入时钟与基本存取', () => {
  test('默认时钟可工作；显式注入函数/对象/数字时钟均可', () => {
    const c1 = new PreflightCache();
    assert.equal(typeof c1.check({
      partition: SITE_A, origin: ORIGIN, url: API, method: 'GET',
    }).checkedAt, 'number');

    const fixed = new PreflightCache(42);
    assert.equal(fixed.check({
      partition: SITE_A, origin: ORIGIN, url: API, method: 'GET',
    }).checkedAt, 42);

    let t = 7;
    const fn = new PreflightCache(() => t);
    assert.equal(fn.check({
      partition: SITE_A, origin: ORIGIN, url: API, method: 'GET',
    }).checkedAt, 7);
    t = 99;
    assert.equal(fn.check({
      partition: SITE_A, origin: ORIGIN, url: API, method: 'GET',
    }).checkedAt, 99);

    const obj = new PreflightCache({ now: () => 123 });
    assert.equal(obj.check({
      partition: SITE_A, origin: ORIGIN, url: API, method: 'GET',
    }).checkedAt, 123);
  });

  test('非法时钟与非法入参会快速抛错', () => {
    assert.throws(() => new PreflightCache('nope'), TypeError);
    const c = new PreflightCache(manualClock());
    assert.throws(() => c.check({ partition: '', origin: ORIGIN, url: API, method: 'GET' }), TypeError);
    assert.throws(() => c.check({ partition: SITE_A, origin: 'not-a-url', url: API, method: 'GET' }), TypeError);
    assert.throws(() => c.check({ partition: SITE_A, origin: ORIGIN, url: API, method: '' }), TypeError);
    assert.throws(() => storeFull(c, { maxAge: -1 }), TypeError);
    assert.throws(() => storeFull(c, { maxAge: NaN }), TypeError);
    assert.throws(() => storeFull(c, { credentials: 'yes' }), TypeError);
    assert.throws(() => storeFull(c, { allowHeaders: [''] }), TypeError);
  });

  test('缺省 maxAge 为标准规定的 5 秒', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    const snap = c.store({
      partition: SITE_A, origin: ORIGIN, url: API, allowMethods: ['PUT'],
    });
    assert.equal(snap.methodExpires.PUT, clock.now() + DEFAULT_MAX_AGE_SECONDS * 1000);
  });
});

describe('可接受结果：方法与全部待检查头同时有效才允许跳过预检', () => {
  test('方法与全部头均在有效期 => ok=true', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    storeFull(c);

    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'PUT', headers: ['X-Custom', 'x-other'],
    });
    assert.equal(res.ok, true);
    assert.equal(res.reason, null);
    assert.deepEqual(res.missingMethods, []);
    assert.deepEqual(res.missingHeaders, []);
    assert.equal(res.matchedEntry.credentials, false);
  });

  test('无待检查头时，只要方法有效即可跳过', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    storeFull(c, { allowMethods: ['POST'], allowHeaders: [] });
    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: API, method: 'POST',
    });
    assert.equal(res.ok, true);
  });

  test('URL 片段（fragment）不参与匹配', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    storeFull(c, { url: `${API}#section-1` });
    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: `${API}#anything`,
      method: 'PUT', headers: ['x-custom', 'x-other'],
    });
    assert.equal(res.ok, true);
  });
});

describe('应被阻止：方法或任一头缺失/过期都不得跳过预检，并指出缺哪项', () => {
  test('无任何缓存条目 => no-entry', () => {
    const c = new PreflightCache(manualClock());
    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: API, method: 'PUT', headers: ['x-custom'],
    });
    assert.equal(res.ok, false);
    assert.equal(res.reason, 'no-entry');
    assert.equal(res.matchedEntry, null);
  });

  test('只缺方法许可 => missingMethods 指出该方法', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    storeFull(c, { allowMethods: ['POST'] }); // 只许可了 POST
    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'PUT', headers: ['x-custom'],
    });
    assert.equal(res.ok, false);
    assert.equal(res.reason, 'missing-permission');
    assert.deepEqual(res.missingMethods, ['PUT']);
    assert.deepEqual(res.missingHeaders, []);
  });

  test('只缺某个头许可 => missingHeaders 指出具体头名', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    storeFull(c, { allowHeaders: ['x-custom'] }); // 只许可了一个头
    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'PUT', headers: ['x-custom', 'x-missing'],
    });
    assert.equal(res.ok, false);
    assert.deepEqual(res.missingMethods, []);
    assert.deepEqual(res.missingHeaders, ['x-missing']);
  });

  test('方法与头同时缺失时两项都要报出来', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    storeFull(c, { allowMethods: ['POST'], allowHeaders: ['x-custom'] });
    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'DELETE', headers: ['x-custom', 'x-absent'],
    });
    assert.equal(res.ok, false);
    assert.deepEqual(res.missingMethods, ['DELETE']);
    assert.deepEqual(res.missingHeaders, ['x-absent']);
  });

  test('方法过期但头仍有效 => 仍阻止，且只报方法', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 2,
      allowMethods: ['PUT'], allowHeaders: [],
    });
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 20,
      allowMethods: [], allowHeaders: ['x-custom'],
    });
    clock.advance(3_000); // 方法许可已过期，头许可仍有效
    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'PUT', headers: ['x-custom'],
    });
    assert.equal(res.ok, false);
    assert.deepEqual(res.missingMethods, ['PUT']);
    assert.deepEqual(res.missingHeaders, []);
  });

  test('maxAge=0 写入即过期：到期时刻严格判定（边界）', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 0,
      allowMethods: ['PUT'], allowHeaders: ['x-custom'],
    });
    const atSameInstant = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'PUT', headers: ['x-custom'],
    });
    assert.equal(atSameInstant.ok, false);
    assert.deepEqual(atSameInstant.missingMethods, ['PUT']);
    assert.deepEqual(atSameInstant.missingHeaders, ['x-custom']);

    clock.advance(1);
    assert.equal(c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'PUT', headers: ['x-custom'],
    }).ok, false);
  });

  test('到期前一毫秒有效、到期当刻失效（边界）', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 1,
      allowMethods: ['PUT'], allowHeaders: ['x-custom'],
    });
    clock.advance(999);
    assert.equal(c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'PUT', headers: ['x-custom'],
    }).ok, true);
    clock.advance(1); // 到达过期时刻
    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'PUT', headers: ['x-custom'],
    });
    assert.equal(res.ok, false);
    assert.deepEqual(res.missingMethods, ['PUT']);
    assert.deepEqual(res.missingHeaders, ['x-custom']);
  });
});

describe('请求头名称大小写等价', () => {
  test('登记与查询的大小写/空白差异不影响匹配，结果统一折叠为小写', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    storeFull(c, {
      allowMethods: ['PUT'],
      allowHeaders: ['X-Custom', '  X-Other  '],
    });
    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'PUT',
      headers: ['x-custom', 'X-OTHER', 'X-CUSTOM'], // 含重复，去重后匹配
    });
    assert.equal(res.ok, true);
    assert.deepEqual(Object.keys(res.matchedEntry.headerExpires).sort(),
      ['x-custom', 'x-other']);
  });
});

describe('通配许可与凭据约束', () => {
  test('无凭据通配方法/头可覆盖普通请求', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 10,
      credentials: false,
      allowMethods: [WILDCARD], allowHeaders: [WILDCARD],
    });
    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'PATCH', headers: ['x-anything', 'y-thing'],
    });
    assert.equal(res.ok, true);
  });

  test('无凭据通配许可不能覆盖带凭据请求（即使条目里有显式许可）', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 10,
      credentials: false,
      allowMethods: ['PUT', WILDCARD],
      allowHeaders: ['x-custom', WILDCARD],
    });
    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      credentials: true, method: 'PUT', headers: ['x-custom'],
    });
    assert.equal(res.ok, false);
    assert.equal(res.reason, 'credentials-mismatch');
    assert.deepEqual(res.missingMethods, ['PUT']);
    assert.deepEqual(res.missingHeaders, ['x-custom']);
    assert.equal(res.matchedEntry.credentials, false);
  });

  test('带凭据条目 + 显式许可可覆盖带凭据请求；通配对带凭据请求无效', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 10,
      credentials: true,
      allowMethods: ['PUT'], allowHeaders: [WILDCARD, 'x-custom'],
    });
    // 带凭据：通配头不生效，但显式 x-custom 生效
    const ok = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      credentials: true, method: 'PUT', headers: ['x-custom'],
    });
    assert.equal(ok.ok, true);

    const blocked = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      credentials: true, method: 'PUT', headers: ['x-not-listed'],
    });
    assert.equal(blocked.ok, false);
    assert.deepEqual(blocked.missingHeaders, ['x-not-listed']);
  });

  test('Authorization 始终需要显式许可：通配头无法覆盖（无凭据也不行）', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 10,
      allowMethods: [WILDCARD], allowHeaders: [WILDCARD],
    });
    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'GET', headers: ['Authorization'], // 任意大小写
    });
    assert.equal(res.ok, false);
    assert.deepEqual(res.missingHeaders, ['authorization']);

    // 补上显式 Authorization 许可后放行
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 10,
      allowHeaders: ['authorization'],
    });
    const res2 = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'GET', headers: ['Authorization'],
    });
    assert.equal(res2.ok, true);
  });

  test('显式 Authorization 许可自身过期后再次被阻止', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 10,
      allowMethods: ['GET'], allowHeaders: [WILDCARD, 'authorization'],
    });
    clock.advance(10_001);
    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'GET', headers: ['authorization'],
    });
    assert.equal(res.ok, false);
    assert.deepEqual(res.missingHeaders, ['authorization']);
  });
});

describe('分区 / 源 / URL 三元组隔离（两个站点嵌入同一页面）', () => {
  test('不同顶层站点分区互不可见，返回 no-entry 能区分缺的是整套许可', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    storeFull(c, { partition: SITE_A });

    const inA = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'PUT', headers: ['x-custom', 'x-other'],
    });
    assert.equal(inA.ok, true);

    const inB = c.check({
      partition: SITE_B, origin: ORIGIN, url: API,
      method: 'PUT', headers: ['x-custom', 'x-other'],
    });
    assert.equal(inB.ok, false);
    assert.equal(inB.reason, 'no-entry');
    assert.equal(inB.matchedEntry, null);
  });

  test('同一分区下不同请求源互不命中', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    storeFull(c, { origin: ORIGIN });
    const res = c.check({
      partition: SITE_A, origin: 'https://other-app.example', url: API,
      method: 'PUT', headers: ['x-custom'],
    });
    assert.equal(res.reason, 'no-entry');
  });

  test('同一分区与源下不同目标 URL 互不命中', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    storeFull(c, { url: API });
    const res = c.check({
      partition: SITE_A, origin: ORIGIN, url: API_OTHER,
      method: 'PUT', headers: ['x-custom'],
    });
    assert.equal(res.reason, 'no-entry');
  });

  test('两个分区各存一份时，check 结果能分别指出各自缺哪项许可', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    // 站点 A：方法 + 一个头
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 10,
      allowMethods: ['PUT'], allowHeaders: ['x-custom'],
    });
    // 站点 B：只有方法，缺全部头
    c.store({
      partition: SITE_B, origin: ORIGIN, url: API, maxAge: 10,
      allowMethods: ['PUT'], allowHeaders: [],
    });
    const a = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'PUT', headers: ['x-custom', 'x-other'],
    });
    const b = c.check({
      partition: SITE_B, origin: ORIGIN, url: API,
      method: 'PUT', headers: ['x-custom', 'x-other'],
    });
    assert.deepEqual(a.missingHeaders, ['x-other']);
    assert.deepEqual(b.missingHeaders, ['x-custom', 'x-other']);
    assert.equal(a.matchedEntry.partition, SITE_A);
    assert.equal(b.matchedEntry.partition, SITE_B);
  });

  test('对象形式分区键按内容隔离', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    c.store({
      partition: { topLevelSite: SITE_A }, origin: ORIGIN, url: API,
      maxAge: 10, allowMethods: ['PUT'],
    });
    assert.equal(c.check({
      partition: { topLevelSite: SITE_A }, origin: ORIGIN, url: API, method: 'PUT',
    }).ok, true);
    assert.equal(c.check({
      partition: { topLevelSite: SITE_B }, origin: ORIGIN, url: API, method: 'PUT',
    }).reason, 'no-entry');
  });
});

describe('缓存命中不得延长寿命', () => {
  test('多次 check 命中后过期时刻保持不变，到期照样失效', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    storeFull(c, { maxAge: 10 });
    const before = c.peek({ partition: SITE_A, origin: ORIGIN, url: API });

    for (let i = 0; i < 5; i++) {
      clock.advance(1_000);
      const hit = c.check({
        partition: SITE_A, origin: ORIGIN, url: API,
        method: 'PUT', headers: ['x-custom', 'x-other'],
      });
      assert.equal(hit.ok, true);
    }
    const after = c.peek({ partition: SITE_A, origin: ORIGIN, url: API });
    assert.deepEqual(after.methodExpires, before.methodExpires);
    assert.deepEqual(after.headerExpires, before.headerExpires);

    clock.set(before.methodExpires.PUT); // 到达原始过期时刻
    const expired = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'PUT', headers: ['x-custom', 'x-other'],
    });
    assert.equal(expired.ok, false);
  });

  test('交错场景：同一字段分两次登记，各自独立过期且互不续期', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    // t0：PUT 与 x-custom 给 5 秒
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 5,
      allowMethods: ['PUT'], allowHeaders: ['x-custom'],
    });
    clock.advance(2_000);
    // t0+2s：POST 与 x-other 给 5 秒（只更新这两个字段）
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 5,
      allowMethods: ['POST'], allowHeaders: ['x-other'],
    });
    const snap = c.peek({ partition: SITE_A, origin: ORIGIN, url: API });
    assert.equal(snap.methodExpires.PUT, 1_000_000 + 5_000);
    assert.equal(snap.methodExpires.POST, 1_002_000 + 5_000);
    assert.equal(snap.headerExpires['x-custom'], 1_000_000 + 5_000);
    assert.equal(snap.headerExpires['x-other'], 1_002_000 + 5_000);

    // t0+5s：第一批字段到期，第二批仍有效
    clock.set(1_005_000);
    const r1 = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'PUT', headers: ['x-custom'],
    });
    assert.equal(r1.ok, false);
    assert.deepEqual(r1.missingMethods, ['PUT']);
    assert.deepEqual(r1.missingHeaders, ['x-custom']);

    const r2 = c.check({
      partition: SITE_A, origin: ORIGIN, url: API,
      method: 'POST', headers: ['x-other'],
    });
    assert.equal(r2.ok, true);

    // 第二批到期后全部失效；purgeExpired 清理空壳条目
    clock.set(1_007_001);
    assert.equal(c.purgeExpired(), 1);
    assert.equal(c.size, 0);
  });

  test('重复预检只更新本次列出的字段，未列出字段保持原过期时刻', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 3,
      allowMethods: ['PUT'], allowHeaders: ['x-custom'],
    });
    clock.advance(1_000);
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 10,
      allowMethods: ['PUT'], allowHeaders: [], // 只续方法，不续头
    });
    const snap = c.peek({ partition: SITE_A, origin: ORIGIN, url: API });
    assert.equal(snap.methodExpires.PUT, 1_001_000 + 10_000);
    assert.equal(snap.headerExpires['x-custom'], 1_000_000 + 3_000);
  });
});

describe('维护操作', () => {
  test('purgeExpired 只清理全字段过期条目，仍有任一字段有效的保留', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API, maxAge: 2,
      allowMethods: ['PUT'],
    });
    c.store({
      partition: SITE_A, origin: ORIGIN, url: API_OTHER, maxAge: 20,
      allowMethods: ['PUT'],
    });
    clock.advance(5_000);
    const removed = c.purgeExpired();
    assert.equal(removed, 1);
    assert.equal(c.size, 1);
    assert.equal(c.peek({
      partition: SITE_A, origin: ORIGIN, url: API_OTHER,
    }) === null, false);
  });

  test('clear 清空全部条目', () => {
    const clock = manualClock();
    const c = new PreflightCache(clock);
    storeFull(c);
    assert.equal(c.size, 1);
    c.clear();
    assert.equal(c.size, 0);
  });
});
