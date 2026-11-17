"""策略参数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class Policy:
    """判定策略。

    :param window_seconds: 风险窗口长度（秒）。
    :param min_documents: 窗口内不同受影响文档达到该数量才产生发现。
    :param min_distinct_dirs: 窗口内独立目录达到该数量才产生发现。
    :param high_entropy_threshold: 高熵判定阈值；熵区间下界达到该值视为高熵。
    :param registered_categories: 已登记文档内容类别集合。
    :param unknown_category: 高熵“未知内容”类别名。
    :param archive_categories: 归档/压缩内容类别（新建归档保留源文档时排除）。
    :param archive_path_suffixes: 归档路径后缀（小写，如 .zip），辅助归档识别。
    :param dir_depth: 独立目录标识的聚合粒度：0 表示使用完整父目录路径；
        k>0 表示只保留父目录路径的最后 k 段（用于跨根目录粗粒度聚合）。
    """

    window_seconds: float = 60.0
    min_documents: int = 3
    min_distinct_dirs: int = 2
    high_entropy_threshold: float = 7.5
    registered_categories: frozenset[str] = frozenset({"document", "office", "pdf"})
    unknown_category: str = "unknown"
    archive_categories: frozenset[str] = frozenset({"archive"})
    archive_path_suffixes: frozenset[str] = frozenset(
        {".zip", ".tar", ".gz", ".7z", ".rar"}
    )
    dir_depth: int = 0

    @staticmethod
    def from_dict(raw: Optional[Mapping[str, Any]]) -> "Policy":
        """从字典构造策略，缺省字段沿用默认值。"""
        if not raw:
            return Policy()

        def _frozen(value: Any, default: frozenset[str]) -> frozenset[str]:
            if value is None:
                return default
            if isinstance(value, str):
                return frozenset({value})
            return frozenset(str(item) for item in value)

        defaults = Policy()
        return Policy(
            window_seconds=float(raw.get("window_seconds", defaults.window_seconds)),
            min_documents=int(raw.get("min_documents", defaults.min_documents)),
            min_distinct_dirs=int(raw.get("min_distinct_dirs", defaults.min_distinct_dirs)),
            high_entropy_threshold=float(
                raw.get("high_entropy_threshold", defaults.high_entropy_threshold)
            ),
            registered_categories=_frozen(
                raw.get("registered_categories"), defaults.registered_categories
            ),
            unknown_category=str(raw.get("unknown_category", defaults.unknown_category)),
            archive_categories=_frozen(
                raw.get("archive_categories"), defaults.archive_categories
            ),
            archive_path_suffixes=_frozen(
                raw.get("archive_path_suffixes"), defaults.archive_path_suffixes
            ),
            dir_depth=int(raw.get("dir_depth", defaults.dir_depth)),
        )
