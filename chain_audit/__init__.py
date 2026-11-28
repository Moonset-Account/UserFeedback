"""证据链核查模块（chain_audit）。

公开 API：

单据（均为不可变 dataclass，数量为整数毫克闭区间）：
    IntakeDoc, SplitDecl, MixDecl, MeasurementDoc,
    DisclosedLinkDoc, BlindGenerationDoc

报告：
    IngestReport, SourceAttribution, SourceShare, ContradictionReport, NodeInfo

主类：
    ChainAuditor
        ingest(*docs) -> IngestReport          # 增量收窄
        node_interval(node_id) -> (lo, hi)     # 节点可行毫克区间
        sources_of(node_id) -> SourceAttribution
        candidate_sources(node_id) -> tuple    # 所有合法来源（含匿名）
        feasible_witness() -> dict | None      # 一份整数可行赋值
        resolve_blind(batch, blind, gen) -> node_id | None
        status / contradiction / documents / node_info

异常：
    InputError  单据格式或结构错误（与“矛盾”区分开）
"""

from .audit import ChainAuditor, SEARCH_BUDGET, SOLVE_BUDGET
from .compiler import ANON
from .model import (
    BlindGenerationDoc,
    ContradictionReport,
    DisclosedLinkDoc,
    GLOBAL_CAP,
    IngestReport,
    InputError,
    IntakeDoc,
    MeasurementDoc,
    MixDecl,
    NodeInfo,
    SourceAttribution,
    SourceShare,
    SplitDecl,
)

__all__ = [
    "ANON",
    "ChainAuditor",
    "SEARCH_BUDGET",
    "SOLVE_BUDGET",
    "BlindGenerationDoc",
    "ContradictionReport",
    "DisclosedLinkDoc",
    "GLOBAL_CAP",
    "IngestReport",
    "InputError",
    "IntakeDoc",
    "MeasurementDoc",
    "MixDecl",
    "NodeInfo",
    "SourceAttribution",
    "SourceShare",
    "SplitDecl",
]
