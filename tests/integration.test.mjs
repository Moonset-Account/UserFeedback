import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { PreflightCache } from '../src/preflight-cache.js';
import { selectUnsafeHeaders } from '../src/cors-safelist.js';

// 端到端组合：先用安全清单挑出需要预检的头，再交给分区缓存判定，
// 覆盖“可接受 / 被阻止 / 边界交错”三类场景在完整调用链上的表现。

function manualClock(start = 5_000_000) {
  let t = start;
  return { now: () => t, advance(ms) { t += ms; } };
}

describe('端到端：安全清单 + 分区预检缓存', () => {
  test('可接受：预检许可覆盖全部不安全头与方法 => 后续请求跳过预检', () => {
    const clock = manualClock();
    const cache = new PreflightCache(clock);

    const requestHeaders = [
      { name: 'Accept', value: '*/*' },                 // 安全，不预检
      { name: 'Content-Type', value: 'application/json' }, // 不安全
      { name: 'X-Trace', value: 'abc' },                // 不安全
    ];
    const unsafe = selectUnsafeHeaders(requestHeaders);
    assert.deepEqual(unsafe, ['content-type', 'x-trace']);

    cache.store({
      partition: 'https://embedder-a.example',
      origin: 'https://app.example',
      url: 'https://api.example/items',
      maxAge: 30,
      allowMethods: ['PUT'],
      allowHeaders: unsafe,
    });

    const decision = cache.check({
      partition: 'https://embedder-a.example',
      origin: 'https://app.example',
      url: 'https://api.example/items',
      method: 'PUT',
      headers: unsafe,
    });
    assert.equal(decision.ok, true);
  });

  test('应被阻止：带凭据请求只登记了无凭据通配许可，且 Authorization 无显式许可', () => {
    const clock = manualClock();
    const cache = new PreflightCache(clock);

    cache.store({
      partition: 'https://embedder-a.example',
      origin: 'https://app.example',
      url: 'https://api.example/items',
      credentials: false,
      maxAge: 30,
      allowMethods: ['*'],
      allowHeaders: ['*'],
    });

    const unsafe = selectUnsafeHeaders([
      { name: 'Authorization', value: 'Bearer token' },
      { name: 'X-Trace', value: 'abc' },
    ]);

    const decision = cache.check({
      partition: 'https://embedder-a.example',
      origin: 'https://app.example',
      url: 'https://api.example/items',
      credentials: true,
      method: 'PUT',
      headers: unsafe,
    });
    assert.equal(decision.ok, false);
    // 凭据门槛优先：无凭据条目整体不能服务带凭据请求
    assert.equal(decision.reason, 'credentials-mismatch');
    assert.deepEqual(decision.missingHeaders, ['authorization', 'x-trace']);
  });

  test('边界交错：时钟推进中，短 TTL 头先到期而长 TTL 方法仍有效，结果精确指出缺项', () => {
    const clock = manualClock();
    const cache = new PreflightCache(clock);
    const key = {
      partition: 'https://embedder-a.example',
      origin: 'https://app.example',
      url: 'https://api.example/items',
    };

    // 第一批：方法与 content-type 给 2 秒
    cache.store({
      ...key, maxAge: 2,
      allowMethods: ['PATCH'],
      allowHeaders: ['content-type'],
    });
    clock.advance(1_000);
    // 第二批：x-trace 给 10 秒
    cache.store({
      ...key, maxAge: 10,
      allowHeaders: ['x-trace'],
    });

    // t=1s：全部有效
    assert.equal(cache.check({
      ...key, method: 'PATCH', headers: ['content-type', 'x-trace'],
    }).ok, true);

    // t=2s：方法与 content-type 到期（严格到期），x-trace 仍有效
    clock.advance(1_000);
    const decision = cache.check({
      ...key, method: 'PATCH', headers: ['content-type', 'x-trace'],
    });
    assert.equal(decision.ok, false);
    assert.equal(decision.reason, 'missing-permission');
    assert.deepEqual(decision.missingMethods, ['PATCH']);
    assert.deepEqual(decision.missingHeaders, ['content-type']);

    // 重新预检只补过期字段；命中过的 x-trace 过期时刻未被延长
    const before = cache.peek(key);
    cache.store({ ...key, maxAge: 20, allowMethods: ['PATCH'], allowHeaders: ['content-type'] });
    const after = cache.peek(key);
    assert.equal(after.headerExpires['x-trace'], before.headerExpires['x-trace']);
    assert.equal(cache.check({
      ...key, method: 'PATCH', headers: ['content-type', 'x-trace'],
    }).ok, true);
  });
});
