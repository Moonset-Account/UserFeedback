import { Clock, TrustedTokenStub, FormValidator, Reason } from '../src/index.js';
import { Scenario, runScenarios } from './harness.js';

/** 只取决定的稳定部分做断言（detail 单独断言，避免噪声） */
const decision = (r) => ({ allowed: r.allowed, reason: r.reason });
const build = (startMs = 0) => {
  const clock = new Clock(startMs);
  const tokenStub = new TrustedTokenStub(clock);
  const validator = new FormValidator({ clock, tokenStub });
  return { clock, tokenStub, validator };
};

/* ------------------------------------------------------------------ */
/* 场景 1：两个有效标签页并发保存草稿；轮换后旧凭证在宽限期内仍可保存 */
/* ------------------------------------------------------------------ */
const scenario1 = new Scenario('两个有效标签页并发保存草稿 + 代际宽限内各自独立保存');
scenario1.run = async () => {
  const { clock, validator } = build(0);
  const SID = 'sess-A';
  validator.createSession(SID);

  // t=0：两个匿名标签页各自拿到绑定同一会话/动作、代际 1 的凭证
  const tabA = validator.issueCredential({ sessionId: SID, action: 'save-draft' });
  const tabB = validator.issueCredential({ sessionId: SID, action: 'save-draft' });
  scenario1.event('签发凭证', { tabA: tabA.generation, tabB: tabB.generation });

  // t=100：两个标签页并发提交保存（validator 的决定是同步原子的，并发调用不会交错污染）
  clock.advance(100);
  const concurrent = await Promise.all([
    Promise.resolve().then(() => validator.validate({
      sessionId: SID, tabId: 'tab-A', action: 'save-draft', token: tabA.token,
      fields: { content: 'A 的第一版草稿' },
    })),
    Promise.resolve().then(() => validator.validate({
      sessionId: SID, tabId: 'tab-B', action: 'save-draft', token: tabB.token,
      fields: { content: 'B 的第一版草稿' },
    })),
  ]);
  scenario1.event('两标签页并发保存完成', { order: ['tab-A', 'tab-B'] });
  scenario1.check('tab-A 首次保存放行', decision(concurrent[0]), { allowed: true, reason: Reason.ALLOWED });
  scenario1.check('tab-B 首次保存放行', decision(concurrent[1]), { allowed: true, reason: Reason.ALLOWED });

  // t=1000：服务端轮换到代际 2；tab-A 立刻拿到新凭证，tab-B 仍持有代际 1 旧凭证
  clock.advance(900);
  const gen2 = validator.rotateGeneration(SID);
  scenario1.event('凭证代际轮换', { newGeneration: gen2, graceMs: 5000 });
  const tabA2 = validator.issueCredential({ sessionId: SID, action: 'save-draft' });

  // t=2000：tab-A 用新代际保存；tab-B 用旧代际保存（距轮换 1000ms，宽限窗口 5000ms 内）
  clock.advance(1000);
  const rA2 = validator.validate({
    sessionId: SID, tabId: 'tab-A', action: 'save-draft', token: tabA2.token,
    fields: { content: 'A 的第二版草稿' },
  });
  const rB2 = validator.validate({
    sessionId: SID, tabId: 'tab-B', action: 'save-draft', token: tabB.token,
    fields: { content: 'B 的第二版草稿' },
  });
  scenario1.event('轮换后再次保存', { tabA: decision(rA2), tabB: decision(rB2) });
  scenario1.check('tab-A 当代际凭证正常放行', decision(rA2), { allowed: true, reason: Reason.ALLOWED });
  scenario1.check('tab-B 上一代凭证宽限期内放行', decision(rB2), { allowed: true, reason: Reason.ALLOWED_GRACE });

  // 草稿按 会话+标签页 隔离：两份草稿各自保留自己的最新内容
  const draftA = validator.getDraft(SID, 'tab-A');
  const draftB = validator.getDraft(SID, 'tab-B');
  scenario1.event('读取两份草稿', { draftA, draftB });
  scenario1.checkTrue('tab-A 草稿独立且为第二版', draftA?.content === 'A 的第二版草稿', { draftA });
  scenario1.checkTrue('tab-B 草稿独立且为第二版', draftB?.content === 'B 的第二版草稿', { draftB });
  scenario1.checkTrue('两份草稿互不覆盖', draftA.content !== draftB.content, { draftA, draftB });
};

/* ------------------------------------------------------------------ */
/* 场景 2：跨动作属性错配——凭证只能用于签发时绑定的动作              */
/* ------------------------------------------------------------------ */
const scenario2 = new Scenario('跨动作属性错配（动作绑定不可挪用）');
scenario2.run = async () => {
  const { tokenStub, validator } = build(0);
  const SID = 'sess-X';
  validator.createSession(SID);
  const draftCred = validator.issueCredential({ sessionId: SID, action: 'save-draft' });
  const submitCred = validator.issueCredential({ sessionId: SID, action: 'submit' });
  scenario2.event('签发两个动作各自绑定的凭证', { draftGen: draftCred.generation, submitGen: submitCred.generation });

  // 草稿凭证挪用于提交动作
  const r1 = validator.validate({
    sessionId: SID, tabId: 'tab-1', action: 'submit', token: draftCred.token,
    fields: { content: 'x', title: 't' },
  });
  scenario2.event('草稿凭证尝试 submit', decision(r1));
  scenario2.check('动作错配被拒绝 (ACTION_MISMATCH)', decision(r1), { allowed: false, reason: Reason.ACTION_MISMATCH });
  scenario2.check('错配决定带绑定证据', r1.detail, {
    stage: 'action-binding', submitted: 'submit', bound: 'save-draft',
  });

  // 提交凭证挪用于草稿动作
  const r2 = validator.validate({
    sessionId: SID, tabId: 'tab-1', action: 'save-draft', token: submitCred.token,
    fields: { content: 'x' },
  });
  scenario2.event('提交凭证尝试 save-draft', decision(r2));
  scenario2.check('反向动作错配同样被拒绝', decision(r2), { allowed: false, reason: Reason.ACTION_MISMATCH });

  // 被篡改的令牌过不了可信桩核验（属性真实性由测试桩保证）
  const tampered = tokenStub.tamper(draftCred.token);
  const r3 = validator.validate({
    sessionId: SID, tabId: 'tab-1', action: 'save-draft', token: tampered,
    fields: { content: 'x' },
  });
  scenario2.event('篡改令牌尝试保存', decision(r3));
  scenario2.check('篡改令牌被拒 (TOKEN_INVALID)', decision(r3), { allowed: false, reason: Reason.TOKEN_INVALID });

  // 对照：动作匹配的合法凭证放行
  const r4 = validator.validate({
    sessionId: SID, tabId: 'tab-1', action: 'save-draft', token: draftCred.token,
    fields: { content: '合法内容' },
  });
  scenario2.event('匹配动作的合法凭证保存', decision(r4));
  scenario2.check('动作匹配时放行（对照组）', decision(r4), { allowed: true, reason: Reason.ALLOWED });

  // 所有拒绝都不得落草稿（只有最后一次合法保存落了一份）
  scenario2.checkTrue('拒绝期间未产生额外草稿', validator.getDraft(SID, 'tab-1')?.content === '合法内容',
    { draft: validator.getDraft(SID, 'tab-1') });
};

/* ------------------------------------------------------------------ */
/* 场景 3：宽限期边界（含临界点、无宽限动作、TTL、跨两代）            */
/* ------------------------------------------------------------------ */
const scenario3 = new Scenario('宽限期边界与时间相关拒绝');
scenario3.run = async () => {
  const { clock, validator } = build(0);
  const SID = 'sess-G';
  validator.createSession(SID);
  // t=0：代际 1 的草稿凭证（长 TTL）与提交凭证
  const draftG1 = validator.issueCredential({ sessionId: SID, action: 'save-draft', ttlMs: 60_000 });
  const submitG1 = validator.issueCredential({ sessionId: SID, action: 'submit', ttlMs: 60_000 });

  // t=1000：轮换到代际 2，宽限截止点 = 1000 + 5000 = 6000
  clock.advance(1000);
  const gen2 = validator.rotateGeneration(SID);
  const draftG2 = validator.issueCredential({ sessionId: SID, action: 'save-draft', ttlMs: 60_000 });
  const shortLivedG2 = validator.issueCredential({ sessionId: SID, action: 'save-draft', ttlMs: 500 }); // exp=1500
  scenario3.event('轮换到代际 2', { generation: gen2, rotatedAt: clock.now(), graceDeadline: 6000 });

  // 临界点 t=6000（恰好等于截止点）：边界在内，旧凭证仍享宽限
  clock.advance(5000);
  const atBoundary = validator.validate({
    sessionId: SID, tabId: 'tab', action: 'save-draft', token: draftG1.token,
    fields: { content: '边界点' },
  });
  scenario3.event('t=6000 临界点使用代际 1 凭证', { now: clock.now(), ...decision(atBoundary) });
  scenario3.check('宽限临界点（含截止点本身）放行', decision(atBoundary), { allowed: true, reason: Reason.ALLOWED_GRACE });

  // t=6001：越过截止点 1ms，宽限失效
  clock.advance(1);
  const pastGrace = validator.validate({
    sessionId: SID, tabId: 'tab', action: 'save-draft', token: draftG1.token,
    fields: { content: '过宽限' },
  });
  scenario3.event('t=6001 使用代际 1 凭证', { now: clock.now(), ...decision(pastGrace) });
  scenario3.check('超出宽限窗口被拒 (GRACE_EXPIRED)', decision(pastGrace), { allowed: false, reason: Reason.GRACE_EXPIRED });

  // 当代际凭证不受影响
  const current = validator.validate({
    sessionId: SID, tabId: 'tab', action: 'save-draft', token: draftG2.token,
    fields: { content: '当代际' },
  });
  scenario3.event('代际 2 凭证保存', decision(current));
  scenario3.check('当代际凭证始终放行', decision(current), { allowed: true, reason: Reason.ALLOWED });

  // submit 动作 graceMs=null：旧代际立即失效，没有宽限
  const staleSubmit = validator.validate({
    sessionId: SID, tabId: 'tab', action: 'submit', token: submitG1.token,
    fields: { content: 'c', title: 't' },
  });
  scenario3.event('代际 1 的 submit 凭证尝试提交', decision(staleSubmit));
  scenario3.check('提交动作不享宽限 (GENERATION_STALE)', decision(staleSubmit), {
    allowed: false, reason: Reason.GENERATION_STALE,
  });

  // 令牌自身 TTL 是独立于宽限期的第二道时间闸门
  const expiredTtl = validator.validate({
    sessionId: SID, tabId: 'tab', action: 'save-draft', token: shortLivedG2.token,
    fields: { content: 'ttl' },
  });
  scenario3.event('当代际但 TTL 已过的凭证', { now: clock.now(), ...decision(expiredTtl) });
  scenario3.check('TTL 过期被拒 (TOKEN_EXPIRED)', decision(expiredTtl), { allowed: false, reason: Reason.TOKEN_EXPIRED });

  // 再轮换一代：代际 1 落后两代，宽限只惠及“上一代”，依旧拒绝
  validator.rotateGeneration(SID);
  const twoGenerationsBehind = validator.validate({
    sessionId: SID, tabId: 'tab', action: 'save-draft', token: draftG1.token,
    fields: { content: '落后两代' },
  });
  scenario3.event('轮换到代际 3 后代际 1 凭证再试', decision(twoGenerationsBehind));
  scenario3.check('落后两代被拒 (GENERATION_STALE)', decision(twoGenerationsBehind), {
    allowed: false, reason: Reason.GENERATION_STALE,
  });
};

/* ------------------------------------------------------------------ */
/* 场景 4：更换会话后旧会话凭证立即失效（无宽限），草稿按会话隔离     */
/* ------------------------------------------------------------------ */
const scenario4 = new Scenario('会话更换后旧代际凭证立即被拒');
scenario4.run = async () => {
  const { clock, validator } = build(0);
  validator.createSession('sess-old');
  const oldCred = validator.issueCredential({ sessionId: 'sess-old', action: 'save-draft' });

  const before = validator.validate({
    sessionId: 'sess-old', tabId: 'tab-A', action: 'save-draft', token: oldCred.token,
    fields: { content: '旧会话草稿' },
  });
  scenario4.event('旧会话内正常保存', decision(before));
  scenario4.check('轮换前旧会话凭证有效', decision(before), { allowed: true, reason: Reason.ALLOWED });

  // t=2000：更换会话——旧会话立即撤销，不给任何宽限
  clock.advance(2000);
  const rotation = validator.rotateSession('sess-old', 'sess-new');
  scenario4.event('更换会话', rotation);

  // 旧凭证 + 旧会话标识：会话已撤销
  const r1 = validator.validate({
    sessionId: 'sess-old', tabId: 'tab-A', action: 'save-draft', token: oldCred.token,
    fields: { content: '尝试写入旧会话' },
  });
  scenario4.event('旧凭证提交到已撤销的旧会话', decision(r1));
  scenario4.check('旧会话被撤销 (SESSION_REVOKED)', decision(r1), { allowed: false, reason: Reason.SESSION_REVOKED });

  // 旧凭证 + 新会话标识：绑定会话不一致
  const r2 = validator.validate({
    sessionId: 'sess-new', tabId: 'tab-A', action: 'save-draft', token: oldCred.token,
    fields: { content: '尝试跨会话' },
  });
  scenario4.event('旧凭证提交到新会话', decision(r2));
  scenario4.check('跨会话使用被拒 (SESSION_MISMATCH)', decision(r2), { allowed: false, reason: Reason.SESSION_MISMATCH });
  scenario4.check('跨会话决定带绑定证据', r2.detail, {
    stage: 'session-binding', submitted: 'sess-new', bound: 'sess-old',
  });

  // 新会话签发的新凭证（代际重新从 1 开始）正常工作
  const newCred = validator.issueCredential({ sessionId: 'sess-new', action: 'save-draft' });
  scenario4.event('新会话签发凭证', { generation: newCred.generation });
  const r3 = validator.validate({
    sessionId: 'sess-new', tabId: 'tab-A', action: 'save-draft', token: newCred.token,
    fields: { content: '新会话草稿' },
  });
  scenario4.event('新会话凭证保存', decision(r3));
  scenario4.check('新会话凭证放行', decision(r3), { allowed: true, reason: Reason.ALLOWED });

  // 草稿隔离：同标签页标识在两个会话下是两份独立草稿，旧草稿未被篡改也未泄漏
  const oldDraft = validator.getDraft('sess-old', 'tab-A');
  const newDraft = validator.getDraft('sess-new', 'tab-A');
  scenario4.event('两个会话下的同名标签页草稿', { oldDraft, newDraft });
  scenario4.checkTrue('旧会话草稿保持撤销前内容', oldDraft?.content === '旧会话草稿', { oldDraft });
  scenario4.checkTrue('新会话草稿独立写入', newDraft?.content === '新会话草稿', { newDraft });
  scenario4.checkTrue('两次拒绝均未污染旧草稿', oldDraft.content === '旧会话草稿', { oldDraft });
};

/* ------------------------------------------------------------------ */
/* 场景 5：表单验证失败后同一标签页用同一凭证合法重试                 */
/* ------------------------------------------------------------------ */
const scenario5 = new Scenario('表单验证失败后的同页合法重试（拒绝不消耗凭证）');
scenario5.run = async () => {
  const { validator } = build(0);
  const SID = 'sess-R';
  validator.createSession(SID);
  const cred = validator.issueCredential({ sessionId: SID, action: 'submit' });
  scenario5.event('签发 submit 凭证', { generation: cred.generation });

  // 第一次：必填字段全缺
  const r1 = validator.validate({
    sessionId: SID, tabId: 'tab-9', action: 'submit', token: cred.token, fields: {},
  });
  scenario5.event('空字段提交', decision(r1));
  scenario5.check('空字段被拒 (FORM_INVALID)', decision(r1), { allowed: false, reason: Reason.FORM_INVALID });
  scenario5.check('返回结构化字段错误', r1.detail.errors.map((e) => e.field), ['content', 'title']);

  // 第二次：补了 content 仍缺 title——同一枚凭证原样重试
  const r2 = validator.validate({
    sessionId: SID, tabId: 'tab-9', action: 'submit', token: cred.token,
    fields: { content: '正文' },
  });
  scenario5.event('缺 title 再次提交（复用同一凭证）', decision(r2));
  scenario5.check('部分缺失仍被拒', decision(r2), { allowed: false, reason: Reason.FORM_INVALID });

  // 字段长度上限
  const r3 = validator.validate({
    sessionId: SID, tabId: 'tab-9', action: 'submit', token: cred.token,
    fields: { content: '正文', title: '标题', comment: 'x'.repeat(501) },
  });
  scenario5.event('comment 超长提交', decision(r3));
  scenario5.check('超长字段被拒 (FORM_INVALID)', decision(r3), { allowed: false, reason: Reason.FORM_INVALID });
  scenario5.check('长度错误带规则细节', r3.detail.errors, [
    { field: 'comment', error: 'maxLength', max: 500 },
  ]);

  // 拒绝不改变代际状态：会话仍停在代际 1，同一枚凭证第三次重试合法数据应放行
  const r4 = validator.validate({
    sessionId: SID, tabId: 'tab-9', action: 'submit', token: cred.token,
    fields: { content: '正文', title: '标题', comment: 'ok' },
  });
  scenario5.event('同一凭证携带合法字段重试', decision(r4));
  scenario5.check('合法重试放行 (ALLOWED)', decision(r4), { allowed: true, reason: Reason.ALLOWED });
  scenario5.check('代际未因失败而推进', r4.detail, {
    generation: 1, currentGeneration: 1, tabId: 'tab-9',
  });

  // submit 不写草稿命名空间
  scenario5.checkTrue('提交动作不产生草稿', validator.getDraft(SID, 'tab-9') === null,
    { draft: validator.getDraft(SID, 'tab-9') });
};

/* ------------------------------------------------------------------ */
/* 入口：全部场景运行完毕后，任何断言失败都以非零退出码结束           */
/* ------------------------------------------------------------------ */
const allPassed = await runScenarios([scenario1, scenario2, scenario3, scenario4, scenario5]);
if (!allPassed) {
  console.error('回归套件存在失败断言，进程以退出码 1 结束。');
  process.exit(1);
}
console.log('全部场景通过，退出码 0。');
