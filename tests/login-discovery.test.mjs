import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createLoginDiscovery, REASONS } from '../src/login-discovery.mjs';

const T = {
  beforeWindow: '2026-05-01T00:00:00Z',
  windowStart: '2026-06-01T00:00:00Z',
  inWindow: '2026-06-15T00:00:00Z',
  windowEnd: '2026-07-01T00:00:00Z',
  afterWindow: '2026-08-01T00:00:00Z',
};

function baseFixture() {
  return {
    ownerships: [
      { domain: 'example.com', orgId: 'org-alpha', status: 'verified' },
      // 子域单独托管：更具体的已验证归属
      { domain: 'mail.example.com', orgId: 'org-beta', status: 'verified' },
      // 归属被标记为冲突
      { domain: 'corp.example.com', orgId: 'org-gamma', status: 'conflicted' },
      // 同一域被两个组织同时 verified：事实冲突
      { domain: 'duo.example.com', orgId: 'org-a', status: 'verified' },
      { domain: 'duo.example.com', orgId: 'org-b', status: 'verified' },
    ],
    teams: [
      { teamId: 'team-alpha', orgId: 'org-alpha', domains: ['example.com'] },
      { teamId: 'team-beta', orgId: 'org-beta', domains: ['mail.example.com'] },
      { teamId: 'team-gamma', orgId: 'org-gamma', domains: ['corp.example.com'] },
      { teamId: 'team-a', orgId: 'org-a', domains: ['duo.example.com'] },
      { teamId: 'team-b', orgId: 'org-b', domains: ['duo.example.com'] },
    ],
    switches: [
      { orgId: 'org-alpha', idp: 'idp-alpha-new', effectiveAt: '2026-01-01T00:00:00Z' },
      { orgId: 'org-beta', idp: 'idp-beta-new', effectiveAt: '2026-01-01T00:00:00Z' },
      { orgId: 'org-gamma', idp: 'idp-gamma-new', effectiveAt: '2026-01-01T00:00:00Z' },
      { orgId: 'org-a', idp: 'idp-a', effectiveAt: '2026-01-01T00:00:00Z' },
      { orgId: 'org-b', idp: 'idp-b', effectiveAt: '2026-01-01T00:00:00Z' },
    ],
    members: [{ email: 'alice@example.com', idp: 'idp-alpha-legacy' }],
    transitionWindows: [
      { teamId: 'team-alpha', startsAt: T.windowStart, endsAt: T.windowEnd },
    ],
  };
}

// ---------- 可接受结果 ----------

test('可接受：子域单独托管时以更具体的已验证归属为准', () => {
  const d = createLoginDiscovery(baseFixture());

  const sub = d.resolve({ email: 'Bob@mail.example.com', at: T.beforeWindow });
  assert.equal(sub.ok, true);
  assert.equal(sub.invitation.teamId, 'team-beta');
  assert.equal(sub.invitation.orgId, 'org-beta');
  assert.equal(sub.invitation.idp, 'idp-beta-new');

  // 父域仍按自身归属解析，不受子域托管影响
  const parent = d.resolve({ email: 'bob@example.com', at: T.beforeWindow });
  assert.equal(parent.ok, true);
  assert.equal(parent.invitation.teamId, 'team-alpha');
  assert.equal(parent.invitation.idp, 'idp-alpha-new');
});

test('可接受：过渡窗口内已登记成员按登记身份源路由，且邀请绑定团队与配置版本', () => {
  const d = createLoginDiscovery(baseFixture());
  const r = d.resolve({ email: 'alice@example.com', at: T.inWindow });
  assert.equal(r.ok, true);
  assert.equal(r.invitation.idp, 'idp-alpha-legacy');
  assert.equal(r.invitation.teamId, 'team-alpha');
  assert.equal(typeof r.invitation.configVersion, 'string');
  assert.equal(r.invitation.issuedAt, new Date(T.inWindow).toISOString());

  // 回执与邀请一致时被接受
  const receipt = d.validateReceipt({
    invitationId: r.invitation.invitationId,
    teamId: r.invitation.teamId,
    idp: r.invitation.idp,
    configVersion: r.invitation.configVersion,
  });
  assert.equal(receipt.ok, true);
});

// ---------- 应被阻止或拒绝的结果 ----------

test('拒绝：归属标记冲突的范围被暂停（含其子域）', () => {
  const d = createLoginDiscovery(baseFixture());

  const direct = d.resolve({ email: 'x@corp.example.com', at: T.beforeWindow });
  assert.equal(direct.ok, false);
  assert.equal(direct.reason, REASONS.OWNERSHIP_SUSPENDED);
  assert.equal(direct.scope, 'corp.example.com');

  // 冲突域的下级子域同样落入被暂停范围
  const nested = d.resolve({ email: 'x@sub.corp.example.com', at: T.beforeWindow });
  assert.equal(nested.ok, false);
  assert.equal(nested.reason, REASONS.OWNERSHIP_SUSPENDED);
});

test('拒绝：同一域被多个组织同时验证归属时暂停对应范围', () => {
  const d = createLoginDiscovery(baseFixture());
  const r = d.resolve({ email: 'x@duo.example.com', at: T.beforeWindow });
  assert.equal(r.ok, false);
  assert.equal(r.reason, REASONS.OWNERSHIP_SUSPENDED);
});

test('拒绝：过渡窗口内未登记成员不得猜测身份源', () => {
  const d = createLoginDiscovery(baseFixture());
  const r = d.resolve({ email: 'carol@example.com', at: T.inWindow });
  assert.equal(r.ok, false);
  assert.equal(r.reason, REASONS.UNREGISTERED_MEMBER);
});

test('拒绝：回执身份源与邀请不一致时被拒', () => {
  const d = createLoginDiscovery(baseFixture());
  const issued = d.resolve({ email: 'alice@example.com', at: T.inWindow });
  assert.equal(issued.ok, true);

  const wrong = d.validateReceipt({
    invitationId: issued.invitation.invitationId,
    teamId: 'team-alpha',
    idp: 'idp-alpha-new', // 窗口内该成员登记的是 legacy
  });
  assert.equal(wrong.ok, false);
  assert.equal(wrong.reason, REASONS.IDP_MISMATCH);

  const wrongTeam = d.validateReceipt({
    invitationId: issued.invitation.invitationId,
    teamId: 'team-beta',
    idp: 'idp-alpha-legacy',
  });
  assert.equal(wrongTeam.ok, false);
  assert.equal(wrongTeam.reason, REASONS.TEAM_MISMATCH);

  const unknown = d.validateReceipt({ invitationId: 'inv-999', teamId: 't', idp: 'i' });
  assert.equal(unknown.ok, false);
  assert.equal(unknown.reason, REASONS.UNKNOWN_INVITATION);
});

// ---------- 边界与交错场景 ----------

test('边界：过渡窗口 [startsAt, endsAt) 的起止时刻路由不同', () => {
  const d = createLoginDiscovery(baseFixture());

  // startsAt 恰好进入窗口：按成员登记
  const atStart = d.resolve({ email: 'alice@example.com', at: T.windowStart });
  assert.equal(atStart.ok, true);
  assert.equal(atStart.invitation.idp, 'idp-alpha-legacy');

  // endsAt 前 1ms 仍在窗口内
  const justBeforeEnd = d.resolve({
    email: 'alice@example.com',
    at: new Date(Date.parse(T.windowEnd) - 1).toISOString(),
  });
  assert.equal(justBeforeEnd.ok, true);
  assert.equal(justBeforeEnd.invitation.idp, 'idp-alpha-legacy');

  // endsAt 恰好离开窗口：回到组织当前身份源
  const atEnd = d.resolve({ email: 'alice@example.com', at: T.windowEnd });
  assert.equal(atEnd.ok, true);
  assert.equal(atEnd.invitation.idp, 'idp-alpha-new');
});

test('交错：签发邀请后发生交接，回执仍须匹配签发时选择的身份源与配置版本', () => {
  const d = createLoginDiscovery({
    ownerships: [{ domain: 'delta.example.com', orgId: 'org-delta', status: 'verified' }],
    teams: [{ teamId: 'team-delta', orgId: 'org-delta', domains: ['delta.example.com'] }],
    switches: [
      { orgId: 'org-delta', idp: 'idp-delta-v1', effectiveAt: '2026-01-01T00:00:00Z' },
    ],
  });

  // 交接前签发邀请
  const before = d.resolve({ email: 'u@delta.example.com', at: '2026-03-01T00:00:00Z' });
  assert.equal(before.ok, true);
  assert.equal(before.invitation.idp, 'idp-delta-v1');
  const versionBefore = before.invitation.configVersion;

  // 交接：组织切换到新身份源
  d.recordSwitch({
    orgId: 'org-delta',
    idp: 'idp-delta-v2',
    effectiveAt: '2026-04-01T00:00:00Z',
  });

  // 交接后的新解析使用新身份源，且配置版本已变化
  const after = d.resolve({ email: 'u@delta.example.com', at: '2026-05-01T00:00:00Z' });
  assert.equal(after.ok, true);
  assert.equal(after.invitation.idp, 'idp-delta-v2');
  assert.notEqual(after.invitation.configVersion, versionBefore);

  // 旧邀请的回执：匹配签发时选择的 idp-delta-v1 → 接受
  const oldReceipt = d.validateReceipt({
    invitationId: before.invitation.invitationId,
    teamId: 'team-delta',
    idp: 'idp-delta-v1',
    configVersion: versionBefore,
  });
  assert.equal(oldReceipt.ok, true);

  // 拿交接后的新身份源冒充旧邀请的回执 → 拒绝
  const replayed = d.validateReceipt({
    invitationId: before.invitation.invitationId,
    teamId: 'team-delta',
    idp: 'idp-delta-v2',
  });
  assert.equal(replayed.ok, false);
  assert.equal(replayed.reason, REASONS.IDP_MISMATCH);

  // 配置版本被篡改 → 拒绝
  const tampered = d.validateReceipt({
    invitationId: before.invitation.invitationId,
    teamId: 'team-delta',
    idp: 'idp-delta-v1',
    configVersion: after.invitation.configVersion,
  });
  assert.equal(tampered.ok, false);
  assert.equal(tampered.reason, REASONS.VERSION_MISMATCH);
});

test('边界：非法邮箱与未知域被拒绝', () => {
  const d = createLoginDiscovery(baseFixture());
  for (const bad of ['not-an-email', 'a@b@c.example.com', '@example.com', 'a@.com']) {
    const r = d.resolve({ email: bad, at: T.beforeWindow });
    assert.equal(r.ok, false, bad);
    assert.equal(r.reason, REASONS.MALFORMED_EMAIL, bad);
  }
  const unknown = d.resolve({ email: 'x@elsewhere.example.org', at: T.beforeWindow });
  assert.equal(unknown.ok, false);
  assert.equal(unknown.reason, REASONS.UNKNOWN_DOMAIN);
});
