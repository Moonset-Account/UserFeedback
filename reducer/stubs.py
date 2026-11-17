"""确定性试验桩与演示样本。

所有桩都是“无外部状态”的确定性函数：同内容样本无论跑多少次、
在哪个进程跑，结论都一致——这样演示才可重放，也能严格区分
“真复现”与“偶发同指纹”。
"""

from __future__ import annotations

from .model import FieldSpec, Record, Sample

TARGET_FINGERPRINT = "FP-PARSER-CRASH-42"


# ---------------------------------------------------------------------
# 演示样本：r_target 是触发解析器异常的关键记录；
# r_mid/r_leaf 通过引用依赖它；r_root/r_extra 与失败无关。
# ---------------------------------------------------------------------
def build_demo_sample() -> Sample:
    return Sample(
        records=[
            Record(
                rid="r_root",
                fields={"title": "root-record"},
                specs={
                    "title": FieldSpec("title", kind="string", min_length=4),
                },
            ),
            Record(
                rid="r_extra",
                fields={"title": "extra-context-record", "note": "noise"},
                specs={
                    "title": FieldSpec("title", kind="string", min_length=4),
                    "note": FieldSpec("note", kind="string", min_length=0, optional=True),
                },
            ),
            Record(
                rid="r_target",
                fields={"payload": "structured-trigger-payload"},
                specs={
                    "payload": FieldSpec("payload", kind="string", min_length=4),
                },
            ),
            Record(
                rid="r_mid",
                fields={"name": "middle", "ref": "r_target"},
                specs={
                    "name": FieldSpec("name", kind="string", min_length=3),
                    "ref": FieldSpec("ref", kind="reference"),
                },
            ),
            Record(
                rid="r_leaf",
                fields={"name": "leaf", "refs": ["r_mid"]},
                specs={
                    "name": FieldSpec("name", kind="string", min_length=2),
                    "refs": FieldSpec("refs", kind="references"),
                },
            ),
            Record(
                rid="r_optional",
                fields={"name": "opty", "comment": "removable-comment"},
                specs={
                    "name": FieldSpec("name", kind="string", min_length=2),
                    "comment": FieldSpec(
                        "comment", kind="string", min_length=0, optional=True
                    ),
                },
            ),
        ]
    )


def _has(sample: Sample, rid: str) -> bool:
    return sample.contains(rid)


# ---------------------------------------------------------------------
# 试验桩
# ---------------------------------------------------------------------
class DependencyTester:
    """确定性桩：完整依赖链 r_leaf -> r_mid -> r_target 同时存在时
    才稳定返回原失败指纹；任一环节缺失都返回另一类指纹。

    因此：单独删除引用方（r_leaf/r_mid）也会破坏复现，而删除
    r_target 必须连带删除依赖闭包——两种方向都能被演示到。
    版本号参与缓存绑定。
    """

    version = "dep-tester-1.0"

    REQUIRED_CHAIN = ("r_leaf", "r_mid", "r_target")

    def run(self, sample: Sample) -> str:
        if all(_has(sample, rid) for rid in self.REQUIRED_CHAIN):
            return TARGET_FINGERPRINT
        return "FP-DIFFERENT-CATEGORY"


class AlwaysTargetTester:
    """任何样本都返回目标指纹，用于演示字段级最小性收敛。"""

    version = "always-target-1.0"

    def run(self, sample: Sample) -> str:
        return TARGET_FINGERPRINT


class FlakyMatchTester:
    """偶发同指纹桩：每三次试验中仅第 2 次返回目标指纹。

    判定完全由已执行试验次数决定（含跨恢复的累计计数），
    因此可重放；required_repeats=3 时永远无法全部复现。
    """

    version = "flaky-tester-1.0"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, sample: Sample) -> str:
        self.calls += 1
        if self.calls % 3 == 2:
            return TARGET_FINGERPRINT
        return "FP-INTERMITTENT-OTHER"


class TimeoutTester:
    """每次试验都超时。"""

    version = "timeout-tester-1.0"

    def run(self, sample: Sample) -> str:
        raise TimeoutError("simulated trial timeout after 30s")


class OtherErrorTester:
    """每次试验都抛出另一种异常（非超时）。"""

    version = "error-tester-1.0"

    def run(self, sample: Sample) -> str:
        raise RuntimeError("simulated non-timeout failure")
