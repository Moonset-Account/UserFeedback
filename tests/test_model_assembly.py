"""model_assembly 的单元测试，全部使用本地合成配置数据。"""

import unittest

from model_assembly import (
    AssemblyConfig,
    Stage,
    StageVersion,
    TensorContract,
    WeightBlock,
    assemble,
    load_config,
)


def contract(dtype, *shape):
    return TensorContract(dtype=dtype, shape=tuple(shape))


def version(stage, name, in_c, out_c, weights=(), workspace=0):
    return StageVersion(
        stage=stage,
        version=name,
        input=in_c,
        output=out_c,
        weights=tuple(WeightBlock(n, b) for n, b in weights),
        workspace_bytes=workspace,
    )


def make_config(stages, limit):
    return AssemblyConfig(stages=tuple(Stage(n, tuple(vs)) for n, vs in stages),
                          vram_limit_bytes=limit)


# 公共张量契约
C_TEXT = contract("float16", 1, 512)
C_EMB_A = contract("float16", 1, 1024)
C_EMB_B = contract("float16", 1, 768)
C_EMB_BF16 = contract("bfloat16", 1, 1024)
C_KEEP = contract("float16", 1, 256)
C_SCORE = contract("float32", 1, 1)


def three_stage_config(limit=10_000, enc_versions=None, filter_versions=None,
                       rank_versions=None):
    """向量编码 -> 候选过滤 -> 排序 的三阶段合成配置。"""
    enc = enc_versions or [
        version("encoder", "enc-v2", C_TEXT, C_EMB_A,
                weights=[("enc_backbone", 1000)], workspace=300),
        version("encoder", "enc-v1", C_TEXT, C_EMB_B,
                weights=[("enc_backbone", 800)], workspace=200),
    ]
    filt = filter_versions or [
        version("filter", "flt-v2", C_EMB_A, C_KEEP,
                weights=[("flt_head", 400)], workspace=500),
        version("filter", "flt-v1", C_EMB_B, C_KEEP,
                weights=[("flt_head", 300)], workspace=150),
    ]
    rank = rank_versions or [
        version("ranker", "rnk-v1", C_KEEP, C_SCORE,
                weights=[("rnk_head", 200)], workspace=100),
    ]
    return make_config(
        [("encoder", enc), ("filter", filt), ("ranker", rank)], limit)


class TestNormalAssembly(unittest.TestCase):
    """正常场景：接口全匹配且在显存上限内。"""

    def test_preferred_versions_selected_and_peak_reported(self):
        # 顶部版本链 enc-v2 -> flt-v2 -> rnk-v1 全部兼容
        cfg = three_stage_config(limit=10_000)
        result = assemble(cfg)
        self.assertTrue(result.ok, result.summary())
        self.assertEqual([v.version for v in result.selection],
                         ["enc-v2", "flt-v2", "rnk-v1"])
        # 权重全部常驻：1000 + 400 + 200 = 1600
        self.assertEqual(result.resident_weight_bytes, 1600)
        # 工作区接续复用：峰值取 max(300, 500, 100) = 500，而非求和 900
        self.assertEqual(result.peak_workspace_bytes, 500)
        self.assertEqual(result.peak_bytes, 2100)

    def test_workspace_reused_not_summed(self):
        # 若工作区求和将超限（1600+900=2500 > 2400），复用则可行（2100 <= 2400）
        cfg = three_stage_config(limit=2400)
        result = assemble(cfg)
        self.assertTrue(result.ok, result.summary())
        self.assertEqual(result.peak_bytes, 2100)

    def test_shared_weight_block_counted_once(self):
        # encoder 与 ranker 共享同名块 "shared_trunk"
        cfg = make_config(
            [
                ("encoder", [version("encoder", "e1", C_TEXT, C_EMB_A,
                                     weights=[("shared_trunk", 700), ("enc_head", 100)],
                                     workspace=50)]),
                ("ranker", [version("ranker", "r1", C_EMB_A, C_SCORE,
                                    weights=[("shared_trunk", 700), ("rnk_head", 60)],
                                    workspace=40)]),
            ],
            limit=10_000,
        )
        result = assemble(cfg)
        self.assertTrue(result.ok, result.summary())
        # 共享块只计一次：700 + 100 + 60 = 860
        self.assertEqual(result.resident_weight_bytes, 860)
        self.assertEqual(result.peak_bytes, 860 + 50)


class TestDowngradeSelection(unittest.TestCase):
    """题面关键场景：某阶段降一版以兼容邻接模型并复用已加载权重。"""

    def test_downgrade_for_adjacent_compat_and_weight_reuse(self):
        # enc-v2 输出 1024 维，但 filter 只接受 768 维 -> 必须降到 enc-v1；
        # enc-v1 与 flt-v1 共享 "backbone" 块，复用后权重更省。
        cfg = make_config(
            [
                ("encoder", [
                    version("encoder", "enc-v2", C_TEXT, C_EMB_A,
                            weights=[("enc_v2_backbone", 1200)], workspace=300),
                    version("encoder", "enc-v1", C_TEXT, C_EMB_B,
                            weights=[("backbone", 900), ("enc_adapter", 50)],
                            workspace=200),
                ]),
                ("filter", [
                    version("filter", "flt-v1", C_EMB_B, C_KEEP,
                            weights=[("backbone", 900), ("flt_head", 100)],
                            workspace=250),
                ]),
                ("ranker", [
                    version("ranker", "rnk-v1", C_KEEP, C_SCORE,
                            weights=[("rnk_head", 80)], workspace=100),
                ]),
            ],
            limit=10_000,
        )
        result = assemble(cfg)
        self.assertTrue(result.ok, result.summary())
        self.assertEqual([v.version for v in result.selection],
                         ["enc-v1", "flt-v1", "rnk-v1"])
        # backbone 共享只计一次：900 + 50 + 100 + 80 = 1130
        self.assertEqual(result.resident_weight_bytes, 1130)
        # 峰值工作区 max(200, 250, 100) = 250
        self.assertEqual(result.peak_workspace_bytes, 250)
        self.assertEqual(result.peak_bytes, 1380)

    def test_downgrade_only_where_needed(self):
        # 字典序偏好：encoder 保持最高版，只有 filter 需要降版
        cfg = three_stage_config(
            limit=10_000,
            filter_versions=[
                version("filter", "flt-v2", C_EMB_B, C_KEEP,   # 只接受 768，与 enc-v2 不兼容
                        weights=[("flt_head", 400)], workspace=500),
                version("filter", "flt-v1", C_EMB_A, C_KEEP,   # 接受 1024，兼容 enc-v2
                        weights=[("flt_head", 300)], workspace=150),
            ],
        )
        result = assemble(cfg)
        self.assertTrue(result.ok, result.summary())
        self.assertEqual([v.version for v in result.selection],
                         ["enc-v2", "flt-v1", "rnk-v1"])


class TestBoundaryAndInterleaving(unittest.TestCase):
    """边界与交错情况。"""

    def test_exact_limit_is_feasible(self):
        # 峰值恰好等于上限：可行（边界含等号）
        cfg = three_stage_config(limit=2100)
        result = assemble(cfg)
        self.assertTrue(result.ok, result.summary())
        self.assertEqual(result.peak_bytes, 2100)

    def test_one_byte_below_limit_uses_lower_peak_combo(self):
        # 顶部组合峰值 2100 超限 1 字节；enc-v1+flt-v1 链峰值更低且可行
        cfg = three_stage_config(limit=2099)
        result = assemble(cfg)
        self.assertTrue(result.ok, result.summary())
        self.assertEqual([v.version for v in result.selection],
                         ["enc-v1", "flt-v1", "rnk-v1"])
        # 800 + 300 + 200 = 1300 权重 + max(200,150,100)=200 工作区 = 1500
        self.assertEqual(result.peak_bytes, 1500)

    def test_single_stage_pipeline(self):
        cfg = make_config(
            [("encoder", [version("encoder", "e1", C_TEXT, C_EMB_A,
                                  weights=[("w", 500)], workspace=120)])],
            limit=620,
        )
        result = assemble(cfg)
        self.assertTrue(result.ok, result.summary())
        self.assertEqual(result.peak_bytes, 620)

    def test_interleaved_chain_no_single_middle_version(self):
        # 交错：A->B 只有 b1 兼容，B->C 只有 b2 兼容，没有任何单个 B 版本
        # 同时满足两端 -> 不可装配，且需报告两端的具体接口冲突。
        cfg = make_config(
            [
                ("a", [version("a", "a1", C_TEXT, C_EMB_A, weights=[("wa", 10)])]),
                ("b", [
                    version("b", "b1", C_EMB_A, C_EMB_B, weights=[("wb1", 10)]),
                    version("b", "b2", C_EMB_BF16, C_KEEP, weights=[("wb2", 10)]),
                ]),
                ("c", [version("c", "c1", C_KEEP, C_SCORE, weights=[("wc", 10)])]),
            ],
            limit=10_000,
        )
        result = assemble(cfg)
        self.assertFalse(result.ok)
        self.assertIsNone(result.memory_conflict)
        described = "\n".join(c.describe() for c in result.interface_conflicts)
        # a1 -> b2 的 dtype 冲突与 b1 -> c1 的 shape 冲突都应被记录
        self.assertIn("a/a1 -> b/b2", described)
        self.assertIn("dtype", described)
        self.assertIn("b/b1 -> c/c1", described)
        self.assertIn("shape", described)


class TestConflictReporting(unittest.TestCase):
    """无法装配时的具体冲突。"""

    def test_total_interface_mismatch_reports_details(self):
        cfg = make_config(
            [
                ("encoder", [version("encoder", "enc-v1", C_TEXT, C_EMB_BF16,
                                     weights=[("w", 10)])]),
                ("filter", [version("filter", "flt-v1", C_EMB_A, C_KEEP,
                                    weights=[("w2", 10)])]),
            ],
            limit=10_000,
        )
        result = assemble(cfg)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.interface_conflicts), 1)
        conflict = result.interface_conflicts[0]
        self.assertEqual(conflict.upstream_stage, "encoder")
        self.assertEqual(conflict.upstream_version, "enc-v1")
        self.assertEqual(conflict.downstream_stage, "filter")
        self.assertEqual(conflict.downstream_version, "flt-v1")
        self.assertIn("bfloat16", conflict.reason)
        self.assertIn("float16", conflict.reason)
        self.assertIn("接口冲突", result.summary())

    def test_memory_conflict_reports_min_peak_and_excess(self):
        # 两条接口全匹配的链：
        #   enc-v2 链峰值 2100，enc-v1 链峰值 1500；上限 1400 -> 均超限
        cfg = three_stage_config(limit=1400)
        result = assemble(cfg)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.memory_conflict)
        mc = result.memory_conflict
        self.assertEqual(mc.limit_bytes, 1400)
        self.assertEqual(mc.best_peak_bytes, 1500)  # 取最低峰值组合
        self.assertEqual(mc.excess_bytes, 100)
        self.assertEqual(mc.best_selection,
                         (("encoder", "enc-v1"), ("filter", "flt-v1"),
                          ("ranker", "rnk-v1")))
        self.assertIn("显存冲突", result.summary())


class TestConfigValidation(unittest.TestCase):
    """配置加载校验。"""

    def test_load_config_roundtrip(self):
        cfg = load_config({
            "vram_limit_bytes": 2048,
            "stages": [
                {"name": "encoder", "versions": [
                    {"version": "e1",
                     "input": {"dtype": "float16", "shape": [1, 512]},
                     "output": {"dtype": "float16", "shape": [1, 1024]},
                     "weights": [{"name": "w", "bytes": 100}],
                     "workspace_bytes": 64},
                ]},
            ],
        })
        self.assertEqual(cfg.vram_limit_bytes, 2048)
        self.assertEqual(cfg.stages[0].name, "encoder")
        self.assertEqual(cfg.stages[0].versions[0].output.shape, (1, 1024))

    def test_empty_stages_rejected(self):
        with self.assertRaises(ValueError):
            load_config({"vram_limit_bytes": 1, "stages": []})

    def test_empty_versions_rejected(self):
        with self.assertRaises(ValueError):
            load_config({"vram_limit_bytes": 1,
                         "stages": [{"name": "s", "versions": []}]})

    def test_duplicate_version_rejected(self):
        with self.assertRaises(ValueError):
            load_config({"vram_limit_bytes": 1, "stages": [
                {"name": "s", "versions": [
                    {"version": "v1",
                     "input": {"dtype": "float16", "shape": [1]},
                     "output": {"dtype": "float16", "shape": [1]},
                     "workspace_bytes": 0},
                    {"version": "v1",
                     "input": {"dtype": "float16", "shape": [1]},
                     "output": {"dtype": "float16", "shape": [1]},
                     "workspace_bytes": 0},
                ]}]})

    def test_inconsistent_shared_block_size_rejected(self):
        with self.assertRaises(ValueError):
            load_config({"vram_limit_bytes": 1, "stages": [
                {"name": "a", "versions": [
                    {"version": "a1",
                     "input": {"dtype": "float16", "shape": [1]},
                     "output": {"dtype": "float16", "shape": [1]},
                     "weights": [{"name": "shared", "bytes": 100}],
                     "workspace_bytes": 0},
                ]},
                {"name": "b", "versions": [
                    {"version": "b1",
                     "input": {"dtype": "float16", "shape": [1]},
                     "output": {"dtype": "float16", "shape": [1]},
                     "weights": [{"name": "shared", "bytes": 200}],
                     "workspace_bytes": 0},
                ]},
            ]})

    def test_bad_contract_rejected(self):
        with self.assertRaises(ValueError):
            load_config({"vram_limit_bytes": 1, "stages": [
                {"name": "a", "versions": [
                    {"version": "a1",
                     "input": {"dtype": "float16", "shape": [1, 0]},
                     "output": {"dtype": "float16", "shape": [1]},
                     "workspace_bytes": 0},
                ]}]})

    def test_negative_limit_rejected(self):
        with self.assertRaises(ValueError):
            load_config({"vram_limit_bytes": -1, "stages": [
                {"name": "a", "versions": [
                    {"version": "a1",
                     "input": {"dtype": "float16", "shape": [1]},
                     "output": {"dtype": "float16", "shape": [1]},
                     "workspace_bytes": 0},
                ]}]})


if __name__ == "__main__":
    unittest.main()
