//! 提交判定器验收测试。
//!
//! 题目指定的三个交错场景：
//! - [`union_majority_is_not_joint_majority`]：只满足并集多数
//! - [`stale_old_term_ack_cannot_advance_commit`]：旧任期确认迟到
//! - [`reopen_before_completion_is_rejected`]：完成条目确认前重开
//!
//! 另有正常路径、多数边界、尾部替换、快照证据保留与重放确定性测试。

use membership_commit::report::{AckState, CommitRule, EffectiveConfig};
use membership_commit::*;

type TestResult = Result<(), DeciderError>;

fn old_group() -> Config {
    Config::new(["A", "B", "C"])
}

fn new_group() -> Config {
    Config::new(["C", "D", "E"])
}

fn ack(node: &str, index: LogIndex, term: Term) -> Ack {
    Ack {
        from: node.to_string(),
        prefix_index: index,
        prefix_term: term,
    }
}

fn report_at<'a>(ev: &'a Evaluation, index: LogIndex) -> &'a EntryReport {
    ev.entries
        .iter()
        .find(|e| e.index == index)
        .unwrap_or_else(|| panic!("日志中不存在下标 {index} 的条目"))
}

fn state_of(report: &EntryReport, node: &str) -> AckState {
    let (_, state) = report
        .ack_states
        .iter()
        .find(|(n, _)| n == node)
        .unwrap_or_else(|| panic!("报告中缺少副本 {node}"));
    *state
}

// ===========================================================================
// 交错一：只满足并集多数，联合阶段仍不能提交
// 旧组 A,B,C，新组 C,D,E；各自多数门槛都是 2。
// ===========================================================================
#[test]
fn union_majority_is_not_joint_majority() -> TestResult {
    // ---- 前置：联合多数真正满足时，提交才能前进，共有成员两边分别计入 ----
    let mut d = Decider::new(2, old_group())?;
    d.append_normal(2)?; // idx1：旧阶段普通条目
    let begin = d.propose(2, new_group())?; // idx2：C_old,new
    assert_eq!(begin, 2);
    d.append_normal(2)?; // idx3：联合阶段普通条目

    // A、B 的确认只到换员开始条目；C、D 到联合阶段新条目；E 无确认。
    d.record_ack(ack("A", 2, 2))?;
    d.record_ack(ack("B", 2, 2))?;
    d.record_ack(ack("C", 3, 2))?;
    d.record_ack(ack("D", 3, 2))?;

    let ev = d.evaluate();
    // 旧组 A,B,C 全覆盖（C 的前缀到 3 自然覆盖 2），新组 C,D 覆盖：两组各 2 票。
    let r2 = report_at(&ev, 2);
    assert_eq!(r2.rule, CommitRule::Joint);
    assert!(r2.old_group.as_ref().unwrap().satisfied);
    assert!(r2.new_group.as_ref().unwrap().satisfied);
    assert!(r2.rule_satisfied);
    assert_eq!(ev.commit_index, 2, "两组多数都满足时换员开始条目才能提交");
    // 共有成员 C 同时计入两组。
    assert_eq!(r2.old_group.as_ref().unwrap().matched, 3);
    assert_eq!(r2.new_group.as_ref().unwrap().matched, 2);
    // idx3 新组同样满足（C、D），但 A、B 前缀只到 2，旧组只有 C 一票 → idx3 卡住。
    let r3 = report_at(&ev, 3);
    assert!(!r3.rule_satisfied);
    assert_eq!(r3.old_group.as_ref().unwrap().missing, vec!["A", "B"]);
    assert_eq!(ev.effective_config, EffectiveConfig::Joint {
        old: old_group(),
        new: new_group()
    });
    assert!(ev.pending_membership_change);

    // ---- 真正的只满足并集多数：旧组 3 票，新组只有 C 一票 ----
    let mut d2 = Decider::new(2, old_group())?;
    d2.append_normal(2)?;
    d2.propose(2, new_group())?; // idx2
    d2.append_normal(2)?; // idx3
    d2.record_ack(ack("A", 3, 2))?;
    d2.record_ack(ack("B", 3, 2))?;
    d2.record_ack(ack("C", 3, 2))?;
    // 并集上有 A、B、C 三个不同确认者，但新组 C,D,E 里只有 C 一人。
    let ev2 = d2.evaluate();
    assert_eq!(ev2.commit_index, 1, "联合规则不满足时提交不得前进");
    assert_eq!(
        ev2.effective_config,
        EffectiveConfig::Old(old_group()),
        "换员开始条目未提交，对外仍是旧配置"
    );
    let r2b = report_at(&ev2, 2);
    assert!(r2b.old_group.as_ref().unwrap().satisfied);
    assert!(!r2b.new_group.as_ref().unwrap().satisfied);
    assert!(!r2b.rule_satisfied);
    assert_eq!(r2b.new_group.as_ref().unwrap().missing, vec!["D", "E"]);
    assert_eq!(r2b.missing_members(), vec!["D", "E"]);

    // 联合阶段新条目 idx3 同样卡在新组，缺口报告一致。
    let r3b = report_at(&ev2, 3);
    assert_eq!(r3b.rule, CommitRule::Joint);
    assert!(!r3b.committed);
    assert_eq!(r3b.missing_members(), vec!["D", "E"]);
    // E 从未确认，状态为 Behind；A/B/C 为 Matching。
    assert_eq!(state_of(r3b, "E"), AckState::Behind);
    assert_eq!(state_of(r3b, "C"), AckState::Matching);

    // 共有成员 C 在两个组里分别计入：旧组 3 票、新组 1 票都含 C。
    assert_eq!(r2b.old_group.as_ref().unwrap().matched, 3);
    assert_eq!(r2b.new_group.as_ref().unwrap().matched, 1);

    // 再补 D 的确认（新组凑齐 C、D 两票），联合规则立即放行。
    d2.record_ack(ack("D", 3, 2))?;
    let ev3 = d2.evaluate();
    assert_eq!(ev3.commit_index, 3);
    assert_eq!(
        ev3.effective_config,
        EffectiveConfig::Joint {
            old: old_group(),
            new: new_group()
        }
    );
    Ok(())
}

// ===========================================================================
// 交错二：旧任期确认迟到，且确认必须匹配条目任期与前缀身份
// ===========================================================================
#[test]
fn stale_old_term_ack_cannot_advance_commit() -> TestResult {
    // 任期 1 的旧领导写了两条但一条都没提交。
    let mut d = Decider::new(2, old_group())?;
    d.append_normal(1)?; // idx1 t1
    d.append_normal(1)?; // idx2 t1
    d.append_normal(2)?; // idx3 t2：新任期条目

    // 旧任期的“迟到确认”：声称前缀到 2、任期为 1。
    // 本地日志 idx2 确实是任期 1，所以身份匹配，但这些条目不是领导本任期的。
    d.record_ack(ack("A", 2, 1))?;
    d.record_ack(ack("B", 2, 1))?;
    let ev = d.evaluate();
    assert_eq!(ev.commit_index, 0, "旧任期条目不能由当前领导直接推进");
    let r1 = report_at(&ev, 1);
    assert!(r1.rule_satisfied, "多数本身是满足的");
    assert!(r1.blocked_by_leader_term, "但被领导任期约束挡住");

    // 错误任期的确认：前缀下标覆盖 idx2，但申报任期与本地不符。
    d.record_ack(ack("C", 2, 9))?; // 本地 idx2 是 t1，申报 t9
    let ev = d.evaluate();
    let r2 = report_at(&ev, 2);
    assert_eq!(state_of(r2, "C"), AckState::TermMismatch);
    assert_eq!(r2.old_group.as_ref().unwrap().matched, 2, "C 的错任期确认不计入");
    assert_eq!(ev.commit_index, 0);

    // 新任期条目拿到旧组多数后，领导才能推进边界，并一次性带上旧任期前缀。
    d.record_ack(ack("A", 3, 2))?;
    d.record_ack(ack("B", 3, 2))?;
    let ev = d.evaluate();
    assert_eq!(ev.commit_index, 3, "本任期条目满足规则，前缀 t1 条目搭车提交");
    assert!(report_at(&ev, 1).committed);
    assert!(report_at(&ev, 2).committed);
    assert!(report_at(&ev, 3).committed);

    // 换员后旧组确认不能推动新条目。
    let mut d2 = Decider::new(2, old_group())?;
    d2.append_normal(2)?; // idx1
    d2.propose(2, new_group())?; // idx2 开始
    d2.append_end_joint(2)?; // idx3 完成
    d2.append_normal(2)?; // idx4：新配置阶段条目

    // 让 idx1..idx3 在联合规则下提交。
    for node in ["A", "B", "C", "D", "E"] {
        d2.record_ack(ack(node, 3, 2))?;
    }
    let ev = d2.evaluate();
    assert_eq!(ev.commit_index, 3);
    assert_eq!(ev.effective_config, EffectiveConfig::New(new_group()));

    // idx4 只有旧组成员确认：新组多数不满足，不能推进。
    d2.record_ack(ack("A", 4, 2))?;
    d2.record_ack(ack("B", 4, 2))?;
    let ev = d2.evaluate();
    assert_eq!(ev.commit_index, 3, "新组启用后旧组确认无效");
    let r4 = report_at(&ev, 4);
    assert_eq!(r4.rule, CommitRule::NewOnly);
    let nq = r4.new_group.as_ref().unwrap();
    assert!(!nq.satisfied);
    assert_eq!(nq.missing, vec!["C", "D", "E"]);
    // A、B 在新组里连投票权都没有。
    assert_eq!(state_of(r4, "A"), AckState::NonVoter);
    assert_eq!(state_of(r4, "B"), AckState::NonVoter);

    // 新组自己的多数（C、D）到达后才能提交 idx4。
    d2.record_ack(ack("C", 4, 2))?;
    d2.record_ack(ack("D", 4, 2))?;
    let ev = d2.evaluate();
    assert_eq!(ev.commit_index, 4);
    Ok(())
}

// ===========================================================================
// 交错三：完成条目确认前重开换员被拒绝；快照保留未完成换员证据
// ===========================================================================
#[test]
fn reopen_before_completion_is_rejected() -> TestResult {
    let mut d = Decider::new(2, old_group())?;
    d.append_normal(2)?; // idx1
    d.propose(2, new_group())?; // idx2 开始，联合阶段
    d.append_normal(2)?; // idx3
    d.append_end_joint(2)?; // idx4 完成（尚未拿到确认）

    // 完成条目确认前重开：拒绝。
    let err = d.propose(2, Config::new(["F", "G"])).unwrap_err();
    assert_eq!(err, DeciderError::JointInProgress);

    // 只让旧组确认到完成条目：联合规则不满足，完成条目不提交，仍不能重开。
    d.record_ack(ack("A", 4, 2))?;
    d.record_ack(ack("B", 4, 2))?;
    assert_eq!(d.evaluate().commit_index, 1);
    let err = d.propose(2, Config::new(["F", "G"])).unwrap_err();
    assert_eq!(err, DeciderError::JointInProgress);

    // 双方多数补齐，完成条目在联合规则下提交，之后才允许新提议。
    d.record_ack(ack("C", 4, 2))?;
    d.record_ack(ack("D", 4, 2))?;
    let ev = d.evaluate();
    assert_eq!(ev.commit_index, 4);
    assert_eq!(ev.effective_config, EffectiveConfig::New(new_group()));
    assert!(!ev.pending_membership_change);
    let next = d.propose(2, Config::new(["F", "G"]))?;
    assert_eq!(next, 5);

    // ---- 快照必须保留未完成换员的证据 ----
    let mut s = Decider::new(2, old_group())?;
    s.append_normal(2)?; // idx1
    s.propose(2, new_group())?; // idx2 开始，未提交
    // idx1 先拿到旧组多数并提交，使得压缩到 1 合法。
    s.record_ack(ack("A", 1, 2))?;
    s.record_ack(ack("B", 1, 2))?;
    assert_eq!(s.evaluate().commit_index, 1);

    let snap = s.compact(1)?;
    assert_eq!(snap.base_index, 1);
    assert_eq!(snap.config, old_group(), "压缩位置仍是旧配置");
    let pending = snap.pending_joint.as_ref().expect("未完成换员证据必须保留");
    assert_eq!(pending.begin_index, 2);
    assert_eq!(pending.old, old_group());
    assert_eq!(pending.new, new_group());

    // 从快照恢复后：联合规则继续生效，重开仍被拒绝，补齐双方多数后可完成。
    let mut restored = Decider::restore(snap, 2);
    assert_eq!(restored.snapshot_base(), 1);
    assert_eq!(
        restored.effective_config(),
        EffectiveConfig::Old(old_group())
    );
    let err = restored
        .propose(2, Config::new(["F", "G"]))
        .unwrap_err();
    assert_eq!(err, DeciderError::JointInProgress);

    // 恢复后日志里已没有 idx2（被压缩边界之后？不——idx2 在基址之后仍保留）。
    // idx2 位于快照基址 1 之后，因此仍在内存日志中，可继续收确认。
    restored.record_ack(ack("A", 2, 2))?;
    restored.record_ack(ack("B", 2, 2))?;
    restored.record_ack(ack("C", 2, 2))?;
    restored.record_ack(ack("D", 2, 2))?;
    let ev = restored.evaluate();
    assert_eq!(ev.commit_index, 2, "快照证据支撑联合规则继续判定");
    assert_eq!(
        ev.effective_config,
        EffectiveConfig::Joint {
            old: old_group(),
            new: new_group()
        }
    );
    Ok(())
}

// ===========================================================================
// 正常路径：完整换员流程逐段核对规则与配置切换
// ===========================================================================
#[test]
fn full_membership_change_happy_path() -> TestResult {
    let mut d = Decider::new(2, old_group())?;
    d.append_normal(2)?; // idx1 旧阶段
    d.propose(2, new_group())?; // idx2 开始
    d.append_normal(2)?; // idx3 联合阶段
    d.append_end_joint(2)?; // idx4 完成
    d.append_normal(2)?; // idx5 新阶段

    // 全体确认到 idx4：联合规则覆盖 idx2..idx4。
    for node in ["A", "B", "C", "D", "E"] {
        d.record_ack(ack(node, 4, 2))?;
    }
    let ev = d.evaluate();
    assert_eq!(ev.commit_index, 4);
    assert_eq!(report_at(&ev, 1).rule, CommitRule::OldOnly);
    assert_eq!(report_at(&ev, 2).rule, CommitRule::Joint);
    assert_eq!(report_at(&ev, 3).rule, CommitRule::Joint);
    assert_eq!(report_at(&ev, 4).rule, CommitRule::Joint);
    assert_eq!(report_at(&ev, 5).rule, CommitRule::NewOnly);
    assert_eq!(ev.effective_config, EffectiveConfig::New(new_group()));

    // idx5 只需新组多数（2 票），C、E 即可。
    d.record_ack(ack("C", 5, 2))?;
    d.record_ack(ack("E", 5, 2))?;
    let ev = d.evaluate();
    assert_eq!(ev.commit_index, 5);
    let r5 = report_at(&ev, 5);
    assert!(r5.rule_satisfied);
    assert_eq!(r5.new_group.as_ref().unwrap().matched, 2);
    assert!(r5.old_group.is_none());
    Ok(())
}

// ===========================================================================
// 边界：多数门槛 floor(n/2)+1，差一票都不行
// ===========================================================================
#[test]
fn majority_boundary_one_vote_short() -> TestResult {
    let mut d = Decider::new(2, old_group())?;
    d.append_normal(2)?; // idx1
    d.record_ack(ack("A", 1, 2))?; // 3 人组需 2 票，只有 1 票
    let ev = d.evaluate();
    assert_eq!(ev.commit_index, 0);
    let q = report_at(&ev, 1).old_group.as_ref().unwrap();
    assert_eq!(q.needed, 2);
    assert_eq!(q.matched, 1);
    assert_eq!(q.missing, vec!["B", "C"]);

    d.record_ack(ack("B", 1, 2))?; // 凑齐第 2 票
    let ev = d.evaluate();
    assert_eq!(ev.commit_index, 1);

    // 偶数人配置（4 人需 3 票）同样校验。
    let four = Config::new(["A", "B", "C", "D"]);
    assert_eq!(four.majority(), 3);
    let mut d2 = Decider::new(1, four)?;
    d2.append_normal(1)?;
    d2.record_ack(ack("A", 1, 1))?;
    d2.record_ack(ack("B", 1, 1))?;
    assert_eq!(d2.evaluate().commit_index, 0);
    d2.record_ack(ack("C", 1, 1))?;
    assert_eq!(d2.evaluate().commit_index, 1);
    Ok(())
}

// ===========================================================================
// 边界：单节点配置，自己一票即多数；空配置拒绝构造
// ===========================================================================
#[test]
fn single_node_and_empty_config() {
    let solo = Config::new(["S"]);
    assert_eq!(solo.majority(), 1);
    let mut d = Decider::new(1, solo).unwrap();
    d.append_normal(1).unwrap();
    d.record_ack(ack("S", 1, 1)).unwrap();
    assert_eq!(d.evaluate().commit_index, 1);

    assert_eq!(
        Decider::new(1, Config::new(Vec::<&str>::new())).unwrap_err(),
        DeciderError::EmptyConfig
    );
}

// ===========================================================================
// 换任期后未提交尾部替换：截断 + 迟到确认失效
// ===========================================================================
#[test]
fn uncommitted_tail_replacement_after_term_change() -> TestResult {
    let mut d = Decider::new(2, old_group())?;
    d.append_normal(2)?; // idx1
    d.append_normal(2)?; // idx2
    d.append_normal(2)?; // idx3
    d.record_ack(ack("A", 1, 2))?;
    d.record_ack(ack("B", 1, 2))?;
    assert_eq!(d.evaluate().commit_index, 1);

    // 新任期从 idx2 起重写未提交尾部。
    d.truncate_uncommitted(2)?;
    assert_eq!(d.last_index(), 1);
    d.append_normal(3)?; // 新 idx2，任期 3

    // 旧确认还停留在 idx1，不影响新条目；错任期的新确认被标记。
    d.record_ack(ack("C", 2, 2))?; // 本地 idx2 是 t3，申报 t2 → 不匹配
    let ev = d.evaluate();
    let r2 = report_at(&ev, 2);
    assert_eq!(state_of(r2, "C"), AckState::TermMismatch);
    assert_eq!(ev.commit_index, 1);

    // 截断已提交部分必须被拒绝。
    assert_eq!(
        d.truncate_uncommitted(1).unwrap_err(),
        DeciderError::TruncateCommitted {
            from: 1,
            commit_index: 1
        }
    );

    // 截断把联合开始条目带走时，未完成换员证据同步清除。
    let mut d2 = Decider::new(2, old_group())?;
    d2.propose(2, new_group())?; // idx1 未提交
    assert!(d2.evaluate().pending_membership_change);
    d2.truncate_uncommitted(1)?;
    let ev = d2.evaluate();
    assert!(!ev.pending_membership_change);
    assert_eq!(ev.effective_config, EffectiveConfig::Old(old_group()));
    // 证据清除后可以重新提议。
    assert_eq!(d2.propose(2, new_group())?, 1);
    Ok(())
}

// ===========================================================================
// 快照边界：未提交日志不能压缩；完成条目压缩后配置推进为新组
// ===========================================================================
#[test]
fn snapshot_compaction_rules() -> TestResult {
    let mut d = Decider::new(2, old_group())?;
    d.append_normal(2)?; // idx1
    d.propose(2, new_group())?; // idx2
    d.record_ack(ack("A", 1, 2))?;
    d.record_ack(ack("B", 1, 2))?;
    assert_eq!(d.evaluate().commit_index, 1);

    // 压缩未提交位置被拒绝。
    assert_eq!(
        d.compact(2).unwrap_err(),
        DeciderError::CompactionBeyondCommit {
            requested: 2,
            commit_index: 1
        }
    );

    // 让换员完整走完，再压缩，快照配置应为新组且无悬挂证据。
    d.append_end_joint(2)?; // idx3
    for node in ["A", "B", "C", "D", "E"] {
        d.record_ack(ack(node, 3, 2))?;
    }
    assert_eq!(d.evaluate().commit_index, 3);
    let snap = d.compact(3)?;
    assert_eq!(snap.config, new_group());
    assert!(snap.pending_joint.is_none());
    assert_eq!(d.snapshot_base(), 3);
    assert!(d.entries().is_empty());

    let mut restored = Decider::restore(snap, 2);
    assert_eq!(
        restored.effective_config(),
        EffectiveConfig::New(new_group())
    );
    restored.append_normal(2)?; // 新日志 idx4，受新组规则约束
    restored.record_ack(ack("D", 4, 2))?;
    restored.record_ack(ack("E", 4, 2))?;
    let ev = restored.evaluate();
    assert_eq!(ev.commit_index, 4);
    assert_eq!(report_at(&ev, 4).rule, CommitRule::NewOnly);
    Ok(())
}

// ===========================================================================
// 重放确定性：同一批输入多次重放结果一致；分步 API 与 replay API 等价
// ===========================================================================
#[test]
fn replay_is_deterministic_and_equivalent() -> TestResult {
    let ops = vec![
        ReplayOp::Append(Entry::normal(2)),
        ReplayOp::Propose(Proposal {
            term: 2,
            new: new_group(),
        }),
        ReplayOp::Append(Entry::normal(2)),
        ReplayOp::Ack(ack("A", 2, 2)),
        ReplayOp::Ack(ack("B", 2, 2)),
        ReplayOp::Ack(ack("C", 3, 2)),
        ReplayOp::Ack(ack("D", 3, 2)),
    ];
    let first = replay(2, old_group(), &ops).unwrap();
    for _ in 0..5 {
        assert_eq!(first, replay(2, old_group(), &ops).unwrap());
    }

    // 分步构造同一历史。
    let mut d = Decider::new(2, old_group())?;
    d.append_normal(2)?;
    d.propose(2, new_group())?;
    d.append_normal(2)?;
    d.record_ack(ack("A", 2, 2))?;
    d.record_ack(ack("B", 2, 2))?;
    d.record_ack(ack("C", 3, 2))?;
    d.record_ack(ack("D", 3, 2))?;
    assert_eq!(first, d.evaluate());
    Ok(())
}

// ===========================================================================
// 结构性错误：任期回退、无开始条目的完成条目、配置不一致
// ===========================================================================
#[test]
fn structural_errors() -> TestResult {
    let mut d = Decider::new(2, old_group())?;
    d.append_normal(2)?;
    assert_eq!(
        d.append_normal(1).unwrap_err(),
        DeciderError::TermRegression { append: 1, last: 2 }
    );

    let mut d2 = Decider::new(2, old_group())?;
    assert_eq!(
        d2.append_end_joint(2).unwrap_err(),
        DeciderError::EndJointWithoutBegin
    );

    // 完成条目的新配置必须等于开始条目声明的配置。
    let mut d3 = Decider::new(2, old_group())?;
    d3.propose(2, new_group())?;
    let bad = Entry::end_joint(2, Config::new(["X", "Y"]));
    assert_eq!(
        d3.append_entry(bad).unwrap_err(),
        DeciderError::ConfigMismatch {
            expected: new_group(),
            got: Config::new(["X", "Y"])
        }
    );
    // 被拒绝的完成条目不留痕迹，合法完成条目仍可追加。
    assert_eq!(d3.append_end_joint(2)?, 2);

    // 直接追加的开始条目，旧配置必须等于当前稳定配置。
    let mut d4 = Decider::new(2, old_group())?;
    let bad_begin = Entry::begin_joint(2, Config::new(["X"]), new_group());
    assert_eq!(
        d4.append_entry(bad_begin).unwrap_err(),
        DeciderError::ConfigMismatch {
            expected: old_group(),
            got: Config::new(["X"])
        }
    );

    // 确认下标不能超出日志、不能落到快照基址之前。
    let mut d5 = Decider::new(2, old_group())?;
    d5.append_normal(2)?;
    assert_eq!(
        d5.record_ack(ack("A", 2, 2)).unwrap_err(),
        DeciderError::UnknownLogIndex { prefix: 2, last: 1 }
    );
    d5.record_ack(ack("A", 1, 2))?;
    d5.record_ack(ack("B", 1, 2))?;
    let snap = d5.compact(1)?;
    let mut restored = Decider::restore(snap, 2);
    // 基址处（下标 1）的确认可用快照保留的 base_term 校验，合法。
    restored.record_ack(ack("C", 1, 2))?;
    // 落到基址之前的确认无法再校验前缀身份。
    assert_eq!(
        restored.record_ack(ack("A", 0, 0)).unwrap_err(),
        DeciderError::PrefixBeforeSnapshot { prefix: 0, base: 1 }
    );
    Ok(())
}
