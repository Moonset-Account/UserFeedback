import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PairingService, PairingError } from '../src/pairing.mjs';

/** 可推进的假时钟。 */
function fakeClock(start = 1_000_000) {
  let t = start;
  return {
    now: () => t,
    advance: (ms) => { t += ms; },
  };
}

/** 按脚本依次返回字节的随机源（用于强制短码再分配场景）。 */
function scriptedRandom(buffers) {
  let i = 0;
  return () => {
    if (i >= buffers.length) throw new Error('random script exhausted');
    return buffers[i++];
  };
}

const CFG = { sessionTtlMs: 10_000, basePollIntervalMs: 1_000, maxPollIntervalMs: 8_000 };

test('可接受结果：完整配对流程，授权结果由对应凭据领取且仅一次', () => {
  const clock = fakeClock();
  const svc = new PairingService({ ...CFG, now: clock.now });

  const screen = svc.createScreenSession();
  assert.match(screen.code, /^[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{6}$/);
  assert.equal(screen.pollToken.length, 48); // 24 字节高熵凭据

  const page = svc.openConfirmPage(screen.code);
  assert.equal(page.sessionId, screen.sessionId);

  const confirmed = svc.confirmAccount(page.confirmTicket, 'account-13961');
  assert.equal(confirmed.accountId, 'account-13961');

  // 第一次轮询：领取授权结果。
  const first = svc.poll(screen.pollToken);
  assert.equal(first.status, 'authorized');
  assert.equal(first.accountId, 'account-13961');
  assert.equal(typeof first.grant, 'string');

  // 间隔到后再轮询：结果已被领取，不可重放。
  clock.advance(CFG.basePollIntervalMs);
  const second = svc.poll(screen.pollToken);
  assert.equal(second.status, 'claimed');
  assert.equal(second.grant, undefined);
});

test('拒绝：无效轮询凭据、无效/复用确认票据、未知短码', () => {
  const clock = fakeClock();
  const svc = new PairingService({ ...CFG, now: clock.now });
  const screen = svc.createScreenSession();
  const page = svc.openConfirmPage(screen.code);

  assert.throws(() => svc.poll('f'.repeat(48)), (e) => e instanceof PairingError && e.code === 'POLL_TOKEN_INVALID');
  assert.throws(() => svc.openConfirmPage('ZZZZZZ'), (e) => e.code === 'CODE_NOT_FOUND');
  assert.throws(() => svc.confirmAccount('0'.repeat(32), 'acct'), (e) => e.code === 'TICKET_INVALID');

  svc.confirmAccount(page.confirmTicket, 'acct');
  // 同一票据不能确认第二次。
  assert.throws(() => svc.confirmAccount(page.confirmTicket, 'acct2'), (e) => e.code === 'TICKET_INVALID');
});

test('交错场景：旧确认页 + 短码再分配给新屏，旧页点击不能把账号配给新屏', () => {
  const clock = fakeClock();
  // 随机脚本：A 的短码/凭据/id、确认票据、B 的短码(与 A 相同)/凭据/id。
  const svc = new PairingService({
    ...CFG,
    now: clock.now,
    random: scriptedRandom([
      Buffer.alloc(6, 0),          // A 短码 -> AAAAAA
      Buffer.alloc(24, 0x0a),      // A pollToken
      Buffer.alloc(8, 0xa0),       // A sessionId
      Buffer.alloc(16, 0x1c),      // 手机确认票据（绑定 A）
      Buffer.alloc(6, 0),          // B 短码 -> 同为 AAAAAA（A 已到期，短码再分配）
      Buffer.alloc(24, 0x0b),      // B pollToken
      Buffer.alloc(8, 0xb0),       // B sessionId
    ]),
  });

  const screenA = svc.createScreenSession();
  const page = svc.openConfirmPage(screenA.code); // 手机在 A 有效期内打开确认页
  assert.equal(page.sessionId, screenA.sessionId);

  clock.advance(CFG.sessionTtlMs + 1); // A 的短码到期

  const screenB = svc.createScreenSession();
  assert.equal(screenB.code, screenA.code); // 新屏拿到同一短码
  assert.notEqual(screenB.sessionId, screenA.sessionId);

  // 旧确认页点击：被拒绝，且绝不能作用到 B 的会话。
  assert.throws(() => svc.confirmAccount(page.confirmTicket, 'attacker-account'),
    (e) => e.code === 'SESSION_EXPIRED');

  const pollB = svc.poll(screenB.pollToken);
  assert.equal(pollB.status, 'pending'); // 新屏未被配给任何账号
  assert.equal(pollB.accountId, undefined);
});

test('边界：到期前一瞬仍可确认，到期时刻会话失效、授权结果不可再领', () => {
  const clock = fakeClock();
  const svc = new PairingService({ ...CFG, now: clock.now });
  const screen = svc.createScreenSession();
  const page = svc.openConfirmPage(screen.code);

  clock.advance(CFG.sessionTtlMs - 1); // 到期前 1ms
  svc.confirmAccount(page.confirmTicket, 'acct'); // 边界内确认成功

  clock.advance(1); // 恰好到达 expiresAt
  const res = svc.poll(screen.pollToken);
  assert.equal(res.status, 'expired'); // 授权结果随会话过期而不可领取
  assert.equal(res.grant, undefined);
});

test('限流：单设备轮询过快则该会话间隔指数退避，其他设备照常', () => {
  const clock = fakeClock();
  const svc = new PairingService({ ...CFG, now: clock.now });
  const fast = svc.createScreenSession();
  const calm = svc.createScreenSession();

  assert.equal(svc.poll(fast.pollToken).status, 'pending'); // t0，首次允许

  clock.advance(500); // 距上次仅 500ms < 1000ms
  const throttled = svc.poll(fast.pollToken);
  assert.equal(throttled.status, 'throttled');
  assert.equal(throttled.retryAfterMs, 2000); // 间隔 1000 -> 2000
  // 其他设备不受影响：calm 首次轮询照常放行。
  assert.equal(svc.poll(calm.pollToken).status, 'pending');

  clock.advance(1000); // fast 仍在退避窗口内（需到 t0+2500）
  const again = svc.poll(fast.pollToken);
  assert.equal(again.status, 'throttled');
  assert.equal(again.retryAfterMs, 4000); // 继续退避 2000 -> 4000
  // calm 按自己的节奏到达下次可查时刻，依旧放行。
  assert.equal(svc.poll(calm.pollToken).status, 'pending');

  clock.advance(4000); // fast 退避窗口结束
  assert.equal(svc.poll(fast.pollToken).status, 'pending');
});

test('边界：恰好到达下次可查时刻不被限流；退避间隔封顶', () => {
  const clock = fakeClock();
  const svc = new PairingService({ ...CFG, now: clock.now });
  const screen = svc.createScreenSession();

  svc.poll(screen.pollToken); // next = t0+1000
  clock.advance(1000); // 恰好等于 nextPollAt
  assert.equal(svc.poll(screen.pollToken).status, 'pending'); // 边界上不算过快

  // 连续过快轮询，间隔按 1s->2s->4s->8s 退避并在 8s 封顶。
  const seen = [];
  for (let i = 0; i < 4; i++) {
    clock.advance(1);
    seen.push(svc.poll(screen.pollToken).retryAfterMs);
  }
  assert.deepEqual(seen, [2000, 4000, 8000, 8000]);
});

test('隔离：授权结果只能由本会话凭据领取，其他会话凭据拿不到', () => {
  const clock = fakeClock();
  const svc = new PairingService({ ...CFG, now: clock.now });
  const s1 = svc.createScreenSession();
  const s2 = svc.createScreenSession();

  const page = svc.openConfirmPage(s1.code);
  svc.confirmAccount(page.confirmTicket, 'acct-1');

  // s2 的凭据只能看到 s2 自己的状态。
  assert.equal(svc.poll(s2.pollToken).status, 'pending');
  // s1 的凭据领取一次后即不可再得。
  assert.equal(svc.poll(s1.pollToken).status, 'authorized');
  clock.advance(CFG.basePollIntervalMs);
  assert.equal(svc.poll(s1.pollToken).status, 'claimed');
});
