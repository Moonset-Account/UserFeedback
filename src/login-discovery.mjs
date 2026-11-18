/**
 * 登录发现模块
 *
 * 输入（构造时注入，均为已验证/已登记的事实数据）：
 *  - ownerships:         组织域名归属记录 [{ domain, orgId, status: 'verified' | 'conflicted' }]
 *  - teams:              团队覆盖范围     [{ teamId, orgId, domains: [..] }]
 *  - switches:           组织授权切换记录 [{ orgId, idp, effectiveAt }]
 *  - members:            成员登记         [{ email, idp }]
 *  - transitionWindows:  旧身份源过渡窗口 [{ teamId, startsAt, endsAt }]
 *
 * 规则：
 *  1. 子域单独托管时，以更具体的已验证归属为准（最长域名后缀匹配）。
 *  2. 归属存在冲突（记录标记 conflicted，或同一最具体域被多个组织 verified）时，暂停对应范围。
 *  3. 团队存在已登记的旧身份源过渡窗口时，窗口期内只能按成员登记决定身份源；
 *     窗口期外按组织授权切换记录确定当前身份源。
 *  4. 已返回的登录邀请绑定确定的团队与配置版本。
 *  5. 交接（授权切换）之后，回执仍需匹配该次邀请所选择的身份源 / 团队 / 配置版本，
 *     而不是按当前配置重新解析。
 */

export const REASONS = Object.freeze({
  MALFORMED_EMAIL: 'malformed-email',
  UNKNOWN_DOMAIN: 'unknown-domain',
  OWNERSHIP_SUSPENDED: 'ownership-suspended',
  NO_TEAM_COVERAGE: 'no-team-coverage',
  AMBIGUOUS_TEAM_COVERAGE: 'ambiguous-team-coverage',
  UNREGISTERED_MEMBER: 'unregistered-member',
  MEMBER_REGISTRATION_CONFLICT: 'member-registration-conflict',
  NO_ACTIVE_AUTHORIZATION: 'no-active-authorization',
  UNKNOWN_INVITATION: 'unknown-invitation',
  TEAM_MISMATCH: 'team-mismatch',
  IDP_MISMATCH: 'idp-mismatch',
  VERSION_MISMATCH: 'version-mismatch',
});

function ts(value) {
  const t = value instanceof Date ? value.getTime() : new Date(value).getTime();
  if (Number.isNaN(t)) throw new TypeError(`invalid time value: ${value}`);
  return t;
}

function normalizeEmail(email) {
  if (typeof email !== 'string') return null;
  const trimmed = email.trim().toLowerCase();
  const at = trimmed.indexOf('@');
  if (at <= 0 || at !== trimmed.lastIndexOf('@')) return null;
  const domain = trimmed.slice(at + 1);
  if (!domain || domain.startsWith('.') || domain.endsWith('.') || !domain.includes('.')) {
    return null;
  }
  return { local: trimmed.slice(0, at), domain };
}

/** owned 是 domain 本身或其父域后缀 */
function domainMatches(owned, domain) {
  return domain === owned || domain.endsWith('.' + owned);
}

function fail(reason, extra = {}) {
  return { ok: false, reason, ...extra };
}

export function createLoginDiscovery({
  ownerships = [],
  teams = [],
  switches = [],
  members = [],
  transitionWindows = [],
} = {}) {
  const state = {
    ownerships: ownerships.map((o) => ({ ...o, domain: o.domain.toLowerCase() })),
    teams: teams.map((t) => ({ ...t, domains: (t.domains ?? []).map((d) => d.toLowerCase()) })),
    switches: [...switches],
    transitionWindows: [...transitionWindows],
    invitations: new Map(),
    invitationSeq: 0,
  };

  // 成员登记索引：email -> Set<idp>
  const memberRegistrations = new Map();
  for (const m of members) {
    const parsed = normalizeEmail(m.email);
    if (!parsed) continue;
    const key = `${parsed.local}@${parsed.domain}`;
    if (!memberRegistrations.has(key)) memberRegistrations.set(key, new Set());
    memberRegistrations.get(key).add(m.idp);
  }

  /** 归属解析：最具体的已验证归属优先；冲突则暂停 */
  function resolveOwnership(domain) {
    const matching = state.ownerships.filter((o) => domainMatches(o.domain, domain));
    if (matching.length === 0) return { status: 'none' };
    const maxLen = Math.max(...matching.map((o) => o.domain.length));
    const mostSpecific = matching.filter((o) => o.domain.length === maxLen);
    const scope = mostSpecific[0].domain;
    if (mostSpecific.some((o) => o.status === 'conflicted')) {
      return { status: 'suspended', scope };
    }
    const verifiedOrgs = new Set(
      mostSpecific.filter((o) => o.status === 'verified').map((o) => o.orgId),
    );
    if (verifiedOrgs.size === 0) return { status: 'none' };
    if (verifiedOrgs.size > 1) return { status: 'suspended', scope };
    return { status: 'ok', orgId: [...verifiedOrgs][0], scope };
  }

  /** 团队覆盖：同组织内按最具体覆盖域选择；并列则视为歧义 */
  function resolveTeam(orgId, domain) {
    const candidates = state.teams.filter(
      (t) => t.orgId === orgId && t.domains.some((d) => domainMatches(d, domain)),
    );
    if (candidates.length === 0) return { status: 'none' };
    const specificity = (t) =>
      Math.max(...t.domains.filter((d) => domainMatches(d, domain)).map((d) => d.length));
    const maxSpec = Math.max(...candidates.map(specificity));
    const best = candidates.filter((t) => specificity(t) === maxSpec);
    if (best.length > 1) return { status: 'ambiguous' };
    return { status: 'ok', team: best[0] };
  }

  /** 组织当前身份源：at 时刻最新一条已生效的授权切换记录 */
  function resolveOrgIdp(orgId, at) {
    const effective = state.switches
      .filter((s) => s.orgId === orgId && ts(s.effectiveAt) <= at)
      .sort((a, b) => ts(a.effectiveAt) - ts(b.effectiveAt));
    if (effective.length === 0) return null;
    return effective[effective.length - 1].idp;
  }

  /** 团队在 at 时刻是否处于过渡窗口内：[startsAt, endsAt) */
  function activeWindow(teamId, at) {
    return (
      state.transitionWindows.find(
        (w) => w.teamId === teamId && ts(w.startsAt) <= at && at < ts(w.endsAt),
      ) ?? null
    );
  }

  /** 配置版本：由 at 时刻已生效的切换记录与全部登记事实确定，交接后版本随之改变 */
  function configVersionAt(at) {
    const effectiveSwitches = state.switches.filter((s) => ts(s.effectiveAt) <= at).length;
    return (
      `cfg-sw${effectiveSwitches}` +
      `-own${state.ownerships.length}` +
      `-team${state.teams.length}` +
      `-win${state.transitionWindows.length}`
    );
  }

  /**
   * 登录发现：解析邮箱应前往的身份源，并签发绑定团队与配置版本的登录邀请。
   * @returns {{ ok: true, invitation: object } | { ok: false, reason: string }}
   */
  function resolve({ email, at }) {
    const now = ts(at);
    const parsed = normalizeEmail(email);
    if (!parsed) return fail(REASONS.MALFORMED_EMAIL);

    const ownership = resolveOwnership(parsed.domain);
    if (ownership.status === 'suspended') {
      return fail(REASONS.OWNERSHIP_SUSPENDED, { scope: ownership.scope });
    }
    if (ownership.status === 'none') return fail(REASONS.UNKNOWN_DOMAIN);

    const teamResult = resolveTeam(ownership.orgId, parsed.domain);
    if (teamResult.status === 'none') return fail(REASONS.NO_TEAM_COVERAGE);
    if (teamResult.status === 'ambiguous') return fail(REASONS.AMBIGUOUS_TEAM_COVERAGE);
    const team = teamResult.team;

    let idp;
    const window = activeWindow(team.teamId, now);
    if (window) {
      // 过渡窗口内：只能按成员登记决定身份源
      const key = `${parsed.local}@${parsed.domain}`;
      const registered = memberRegistrations.get(key);
      if (!registered) return fail(REASONS.UNREGISTERED_MEMBER, { teamId: team.teamId });
      if (registered.size > 1) {
        return fail(REASONS.MEMBER_REGISTRATION_CONFLICT, { teamId: team.teamId });
      }
      idp = [...registered][0];
    } else {
      idp = resolveOrgIdp(ownership.orgId, now);
      if (idp === null) return fail(REASONS.NO_ACTIVE_AUTHORIZATION);
    }

    state.invitationSeq += 1;
    const invitation = {
      invitationId: `inv-${state.invitationSeq}`,
      email: `${parsed.local}@${parsed.domain}`,
      teamId: team.teamId,
      orgId: ownership.orgId,
      idp,
      configVersion: configVersionAt(now),
      issuedAt: new Date(now).toISOString(),
    };
    state.invitations.set(invitation.invitationId, invitation);
    return { ok: true, invitation };
  }

  /**
   * 回执校验：交接之后，回执仍需匹配该次邀请所绑定的团队、身份源与配置版本。
   * 注意：不按当前配置重新解析，只对照签发时的邀请记录。
   */
  function validateReceipt({ invitationId, teamId, idp, configVersion }) {
    const invitation = state.invitations.get(invitationId);
    if (!invitation) return fail(REASONS.UNKNOWN_INVITATION);
    if (teamId !== invitation.teamId) return fail(REASONS.TEAM_MISMATCH);
    if (idp !== invitation.idp) return fail(REASONS.IDP_MISMATCH);
    if (configVersion !== undefined && configVersion !== invitation.configVersion) {
      return fail(REASONS.VERSION_MISMATCH);
    }
    return { ok: true, invitation };
  }

  /** 追加一条组织授权切换记录（交接），影响后续解析的配置版本与身份源 */
  function recordSwitch(record) {
    state.switches.push({ ...record });
  }

  return { resolve, validateReceipt, recordSwitch, configVersionAt };
}
