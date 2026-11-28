"""本地合成数据：供各测试用例共享的规则/观测/旧结论构建器。

所有数据均为虚构的应急复盘场景，不连接任何外部系统。
"""

T0 = "2026-03-01T00:00:00Z"
T1 = "2026-03-02T00:00:00Z"
T2 = "2026-03-03T00:00:00Z"
T3 = "2026-03-04T00:00:00Z"
T4 = "2026-03-05T00:00:00Z"
T5 = "2026-03-06T00:00:00Z"
T6 = "2026-03-07T00:00:00Z"


def req(fact, min_level="confirmed", independent_source=False):
    return {
        "fact": fact,
        "min_level": min_level,
        "independent_source": independent_source,
    }


def old_rule(rule_id, conclusion_id, label, requires):
    return {
        "rule_id": rule_id,
        "kind": "legacy",
        "conclusion_id": conclusion_id,
        "label": label,
        "requires": requires,
    }


def conclusion_spec(conclusion_id, label, requires):
    return {"conclusion_id": conclusion_id, "label": label, "requires": requires}


def replaces_rule(rule_id, old_id, spec):
    return {"rule_id": rule_id, "kind": "replaces", "replaces": old_id, "conclusion": spec}


def split_rule(rule_id, old_id, specs):
    return {
        "rule_id": rule_id,
        "kind": "split",
        "splits_from": old_id,
        "conclusions": specs,
    }


def merge_rule(rule_id, merge_ids, branches, merged_conclusion=None):
    rule = {
        "rule_id": rule_id,
        "kind": "merge",
        "merges": merge_ids,
        "branches": branches,
    }
    if merged_conclusion is not None:
        rule["conclusion"] = merged_conclusion
    return rule


def obs(obs_id, fact, source_id, level, valid_from=T0, valid_to=None,
        supersedes=None, corrected_at=None):
    return {
        "obs_id": obs_id,
        "fact": fact,
        "source_id": source_id,
        "evidence_level": level,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "supersedes": supersedes,
        "corrected_at": corrected_at,
    }


def legacy(rule_id, supporting, valid_from=T0, valid_to=None):
    return {
        "legacy_rule_id": rule_id,
        "verdict": "established",
        "valid_from": valid_from,
        "valid_to": valid_to,
        "supporting_obs_ids": list(supporting),
    }


# ---------------------------------------------------------------------------
# 场景 1：一对一替换，证据达标 -> 可直接迁移
# ---------------------------------------------------------------------------


def scenario_replaces_ok():
    old_rules = [
        old_rule(
            "OLD-A", "legacy-a", "旧版-A 结论",
            [req("fact-a", "preliminary")],
        )
    ]
    new_rules = [
        replaces_rule(
            "NEW-A", "OLD-A",
            conclusion_spec("new-a", "新版-A 结论", [req("fact-a", "confirmed")]),
        )
    ]
    observations = [
        obs("obs-a1", "fact-a", "src-1", "confirmed"),
    ]
    legacy_conclusions = [legacy("OLD-A", ["obs-a1"])]
    return old_rules, new_rules, legacy_conclusions, observations


# ---------------------------------------------------------------------------
# 场景 2：一对一替换，证据等级不足 -> 保留 + 复核
# ---------------------------------------------------------------------------


def scenario_replaces_below_threshold():
    old_rules = [
        old_rule("OLD-B", "legacy-b", "旧版-B 结论", [req("fact-b", "initial")])
    ]
    new_rules = [
        replaces_rule(
            "NEW-B", "OLD-B",
            conclusion_spec("new-b", "新版-B 结论", [req("fact-b", "official")]),
        )
    ]
    observations = [
        obs("obs-b1", "fact-b", "src-1", "preliminary"),
    ]
    legacy_conclusions = [legacy("OLD-B", ["obs-b1"])]
    return old_rules, new_rules, legacy_conclusions, observations


# ---------------------------------------------------------------------------
# 场景 3：拆分，独立来源齐备 -> 子结论全部迁移
# ---------------------------------------------------------------------------


def scenario_split_ok():
    old_rules = [
        old_rule(
            "OLD-S", "legacy-s", "旧版-S 结论",
            [req("fact-s1"), req("fact-s2")],
        )
    ]
    new_rules = [
        split_rule(
            "NEW-S", "OLD-S",
            [
                conclusion_spec(
                    "new-s1", "拆分子结论-1",
                    [req("fact-s1", "confirmed", independent_source=True)],
                ),
                conclusion_spec(
                    "new-s2", "拆分子结论-2",
                    [req("fact-s2", "confirmed", independent_source=True)],
                ),
            ],
        )
    ]
    observations = [
        obs("obs-s1", "fact-s1", "src-1", "confirmed"),
        obs("obs-s2", "fact-s2", "src-2", "confirmed"),
    ]
    legacy_conclusions = [legacy("OLD-S", ["obs-s1", "obs-s2"])]
    return old_rules, new_rules, legacy_conclusions, observations


# ---------------------------------------------------------------------------
# 场景 4：拆分，独立来源冲突（两个事实只有同一来源）-> 保留 + 复核
# ---------------------------------------------------------------------------


def scenario_split_same_source():
    old_rules = [
        old_rule(
            "OLD-SX", "legacy-sx", "旧版-SX 结论",
            [req("fact-x1"), req("fact-x2")],
        )
    ]
    new_rules = [
        split_rule(
            "NEW-SX", "OLD-SX",
            [
                conclusion_spec(
                    "new-sx1", "拆分子结论-X1",
                    [req("fact-x1", "confirmed", independent_source=True)],
                ),
                conclusion_spec(
                    "new-sx2", "拆分子结论-X2",
                    [req("fact-x2", "confirmed", independent_source=True)],
                ),
            ],
        )
    ]
    observations = [
        obs("obs-x1", "fact-x1", "src-same", "confirmed"),
        obs("obs-x2", "fact-x2", "src-same", "confirmed"),
        # 另有 src-other 的低等级观测，不能用来解独立来源约束
        obs("obs-x1-low", "fact-x1", "src-other", "preliminary"),
    ]
    legacy_conclusions = [legacy("OLD-SX", ["obs-x1", "obs-x2"])]
    return old_rules, new_rules, legacy_conclusions, observations


# ---------------------------------------------------------------------------
# 场景 5：合并，互斥分支同时成立 -> uncertain
# ---------------------------------------------------------------------------


def scenario_merge_uncertain():
    old_rules = [
        old_rule("OLD-M1", "legacy-m1", "互斥解释一", [req("fact-m1")]),
        old_rule("OLD-M2", "legacy-m2", "互斥解释二", [req("fact-m2")]),
    ]
    branches = [
        conclusion_spec("branch-m1", "分支一", [req("fact-m1", "confirmed")]),
        conclusion_spec("branch-m2", "分支二", [req("fact-m2", "confirmed")]),
    ]
    new_rules = [
        merge_rule(
            "NEW-M", ["OLD-M1", "OLD-M2"], branches,
            merged_conclusion=conclusion_spec(
                "new-m", "合并结论（不确定）",
                [req("fact-m1", "confirmed"), req("fact-m2", "confirmed")],
            ),
        )
    ]
    observations = [
        obs("obs-m1", "fact-m1", "src-1", "confirmed"),
        obs("obs-m2", "fact-m2", "src-2", "confirmed"),
    ]
    legacy_conclusions = [
        legacy("OLD-M1", ["obs-m1"]),
        legacy("OLD-M2", ["obs-m2"]),
    ]
    return old_rules, new_rules, legacy_conclusions, observations


# ---------------------------------------------------------------------------
# 场景 6：合并，仅一个分支成立 -> 迁移成立分支
# ---------------------------------------------------------------------------


def scenario_merge_single_branch():
    old_rules = [
        old_rule("OLD-N1", "legacy-n1", "互斥解释一", [req("fact-n1")]),
        old_rule("OLD-N2", "legacy-n2", "互斥解释二", [req("fact-n2")]),
    ]
    branches = [
        conclusion_spec("branch-n1", "分支一", [req("fact-n1", "confirmed")]),
        conclusion_spec("branch-n2", "分支二", [req("fact-n2", "confirmed")]),
    ]
    new_rules = [
        merge_rule("NEW-N", ["OLD-N1", "OLD-N2"], branches)
    ]
    observations = [
        obs("obs-n1", "fact-n1", "src-1", "confirmed"),
        # fact-n2 只有低等级观测，分支二不成立
        obs("obs-n2", "fact-n2", "src-2", "initial"),
    ]
    legacy_conclusions = [
        legacy("OLD-N1", ["obs-n1"]),
        legacy("OLD-N2", ["obs-n2"]),
    ]
    return old_rules, new_rules, legacy_conclusions, observations


# ---------------------------------------------------------------------------
# 场景 7：更正边界 —— 更正生效点切分时段，前段保留、后段重算
# ---------------------------------------------------------------------------


def scenario_correction_boundary():
    old_rules = [
        old_rule("OLD-C", "legacy-c", "旧版-C 结论", [req("fact-c")])
    ]
    new_rules = [
        replaces_rule(
            "NEW-C", "OLD-C",
            conclusion_spec("new-c", "新版-C 结论", [req("fact-c", "confirmed")]),
        )
    ]
    observations = [
        obs(
            "obs-c1", "fact-c", "src-1", "initial",
            valid_from=T0, valid_to=T2,
        ),
        obs(
            "obs-c2", "fact-c", "src-2", "official",
            valid_from=T2, supersedes="obs-c1", corrected_at=T2,
        ),
    ]
    legacy_conclusions = [
        legacy("OLD-C", ["obs-c1"], valid_from=T0, valid_to=T4)
    ]
    return old_rules, new_rules, legacy_conclusions, observations


# ---------------------------------------------------------------------------
# 场景 8：无承接规则 -> 整段保留
# ---------------------------------------------------------------------------


def scenario_no_mapping():
    old_rules = [
        old_rule("OLD-Z", "legacy-z", "已废弃规则结论", [req("fact-z")]),
        old_rule("OLD-OTHER", "legacy-other", "另一旧规则", [req("fact-a")]),
    ]
    new_rules = [
        replaces_rule(
            "NEW-A", "OLD-OTHER",
            conclusion_spec("new-a", "无关注入结论", [req("fact-a")]),
        )
    ]
    observations = [
        obs("obs-z1", "fact-z", "src-1", "confirmed"),
        obs("obs-a1", "fact-a", "src-9", "confirmed"),
    ]
    legacy_conclusions = [legacy("OLD-Z", ["obs-z1"])]
    return old_rules, new_rules, legacy_conclusions, observations


# ---------------------------------------------------------------------------
# 场景 9：交错 —— 同一批输入中并存替换、拆分、合并与更正
# ---------------------------------------------------------------------------


def scenario_interleaved():
    old_rules = [
        old_rule("OLD-A", "legacy-a", "替换类结论", [req("fact-a")]),
        old_rule(
            "OLD-S", "legacy-s", "拆分类结论",
            [req("fact-s1"), req("fact-s2")],
        ),
        old_rule("OLD-M1", "legacy-m1", "互斥解释一", [req("fact-m1")]),
        old_rule("OLD-M2", "legacy-m2", "互斥解释二", [req("fact-m2")]),
    ]
    new_rules = [
        replaces_rule(
            "NEW-A", "OLD-A",
            conclusion_spec("new-a", "新版-A", [req("fact-a", "confirmed")]),
        ),
        split_rule(
            "NEW-S", "OLD-S",
            [
                conclusion_spec(
                    "new-s1", "子结论-1",
                    [req("fact-s1", "confirmed", independent_source=True)],
                ),
                conclusion_spec(
                    "new-s2", "子结论-2",
                    [req("fact-s2", "confirmed", independent_source=True)],
                ),
            ],
        ),
        merge_rule(
            "NEW-M", ["OLD-M1", "OLD-M2"],
            [
                conclusion_spec("branch-m1", "分支一", [req("fact-m1", "confirmed")]),
                conclusion_spec("branch-m2", "分支二", [req("fact-m2", "confirmed")]),
            ],
        ),
    ]
    observations = [
        # 替换类：obs-a1 在 T3 被更正，等级由 preliminary 升到 official
        obs(
            "obs-a1", "fact-a", "src-1", "preliminary",
            valid_from=T0, valid_to=T3,
        ),
        obs(
            "obs-a2", "fact-a", "src-1b", "official",
            valid_from=T3, supersedes="obs-a1", corrected_at=T3,
        ),
        # 拆分类：独立来源齐备
        obs("obs-s1", "fact-s1", "src-2", "confirmed"),
        obs("obs-s2", "fact-s2", "src-3", "confirmed"),
        # 合并类：两个互斥分支都成立
        obs("obs-m1", "fact-m1", "src-4", "confirmed"),
        obs("obs-m2", "fact-m2", "src-5", "confirmed"),
    ]
    legacy_conclusions = [
        legacy("OLD-A", ["obs-a1"], valid_from=T0, valid_to=T5),
        legacy("OLD-S", ["obs-s1", "obs-s2"], valid_from=T0, valid_to=T5),
        legacy("OLD-M1", ["obs-m1"], valid_from=T0, valid_to=T5),
        legacy("OLD-M2", ["obs-m2"], valid_from=T0, valid_to=T5),
    ]
    return old_rules, new_rules, legacy_conclusions, observations


# ---------------------------------------------------------------------------
# 场景 10：多次更正形成链，两个更正点把时段切成三段
# ---------------------------------------------------------------------------


def scenario_double_correction():
    old_rules = [
        old_rule("OLD-D", "legacy-d", "旧版-D 结论", [req("fact-d")])
    ]
    new_rules = [
        replaces_rule(
            "NEW-D", "OLD-D",
            conclusion_spec("new-d", "新版-D", [req("fact-d", "confirmed")]),
        )
    ]
    observations = [
        obs(
            "obs-d1", "fact-d", "src-1", "initial",
            valid_from=T0, valid_to=T2,
        ),
        obs(
            "obs-d2", "fact-d", "src-2", "preliminary",
            valid_from=T2, valid_to=T4,
            supersedes="obs-d1", corrected_at=T2,
        ),
        obs(
            "obs-d3", "fact-d", "src-3", "official",
            valid_from=T4,
            supersedes="obs-d2", corrected_at=T4,
        ),
    ]
    legacy_conclusions = [
        legacy("OLD-D", ["obs-d1"], valid_from=T0, valid_to=T6)
    ]
    return old_rules, new_rules, legacy_conclusions, observations


# ---------------------------------------------------------------------------
# 场景 11：更正点恰好在结论边界上（valid_from 之前 / valid_to 之后）
# ---------------------------------------------------------------------------


def scenario_correction_outside_period():
    old_rules = [
        old_rule("OLD-E", "legacy-e", "旧版-E 结论", [req("fact-e")])
    ]
    new_rules = [
        replaces_rule(
            "NEW-E", "OLD-E",
            conclusion_spec("new-e", "新版-E", [req("fact-e", "confirmed")]),
        )
    ]
    observations = [
        # 结论生效前已更正：整个时段按更正后观测求值
        obs(
            "obs-e1", "fact-e", "src-1", "initial",
            valid_from=T0, valid_to=T1,
        ),
        obs(
            "obs-e2", "fact-e", "src-2", "official",
            valid_from=T1, supersedes="obs-e1", corrected_at=T1,
        ),
    ]
    legacy_conclusions = [
        legacy("OLD-E", ["obs-e1"], valid_from=T2, valid_to=T4)
    ]
    return old_rules, new_rules, legacy_conclusions, observations
