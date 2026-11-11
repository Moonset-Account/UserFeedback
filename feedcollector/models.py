"""采集过程中使用的数据模型（纯数据结构，可序列化为 JSON）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# 支持的 XML 命名空间
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "rss": "",  # RSS 2.0 元素无命名空间
    "media": "http://search.yahoo.com/mrss/",
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# 链接用途（link relation）的中文解释，供页面展示“各链接用途”
LINK_PURPOSE_LABELS = {
    "alternate": "alternate（条目/订阅源的规范 HTML 页面，permalink 候选）",
    "self": "self（订阅源自身地址，用于 feed 身份）",
    "enclosure": "enclosure（附件，即音频媒体地址）",
    "related": "related（相关资源）",
    "via": "via（信息来源）",
    "payment": "payment（付费/赞助入口，非媒体）",
    "icon": "icon（站点图标）",
    "logo": "logo（订阅源 Logo/封面候选）",
    "edit": "edit（编辑接口，采集器不跟随）",
    "unknown": "未识别/缺失 rel 属性（按规范仅作记录，不臆测用途）",
}


@dataclass
class LinkRecord:
    """一条 <link> 记录，保留原始属性与解析依据。"""
    raw_href: str                      # XML 中的原始 href（可能是相对地址）
    resolved_url: str                  # 经 xml:base 链解析后的绝对地址
    rel: str                           # 规范化后的关系名
    rel_raw: str | None                # XML 中原始 rel（缺失为 None）
    link_type: str | None              # type 属性，如 audio/mpeg
    title: str | None
    lang: str | None
    is_permalink: bool                 # RSS guid isPermaLink="true" 的标记
    base_chain: list[str]              # 解析该 URL 时经过的 xml:base 链
    purpose_label: str                 # 面向页面的用途解释
    source_fragment: str               # 该元素的原始 XML 片段（证据）


@dataclass
class EnclosureRecord:
    """音频附件记录及其可用性探测结果。"""
    raw_url: str
    resolved_url: str
    media_type: str | None
    length: int | None                 # 声明字节数，缺失为 None
    origin: str                        # rss:enclosure / atom:link@rel=enclosure / media:content
    base_chain: list[str]
    source_fragment: str
    # 可用性探测结果（probe 前为 None）
    available: bool | None = None
    http_status: int | None = None
    content_type: str | None = None
    content_length: int | None = None
    redirect_chain: list[str] = field(default_factory=list)
    elapsed_ms: int | None = None
    probe_method: str | None = None    # HEAD / GET(range) / FILE / NONE
    error: str | None = None           # 不可用/失败的具体原因，页面直接展示


@dataclass
class CoverRecord:
    """封面记录及与条目/订阅源的归属关系。"""
    owner: str                         # "feed" 或 "entry"
    origin: str                        # itunes:image / media:thumbnail / atom:logo / atom:icon
    raw_url: str
    resolved_url: str
    base_chain: list[str]
    source_fragment: str
    available: bool | None = None
    http_status: int | None = None
    content_type: str | None = None
    error: str | None = None


@dataclass
class IdentityEvidence:
    """一条身份线索及其优先级解释。"""
    tier: int                          # P1..P5
    key: str                           # 规范化后的身份键
    raw_value: str                     # 文档中的原始值
    element: str                       # 来源元素，如 atom:id / guid
    is_permalink: bool | None
    explanation: str                   # 为什么是这个优先级（可解释）
    source_fragment: str


@dataclass
class ParsedEntry:
    """单个条目的解析结果（不含跨条目/跨版本判定）。"""
    feed_url: str
    feed_identity_key: str
    feed_identity_tier: int
    entry_index: int                   # 在文档中的出现顺序
    title: str | None
    published: str | None              # 原始日期文本
    updated: str | None
    authors: list[str]
    summary: str | None
    links: list[LinkRecord]
    enclosures: list[EnclosureRecord]
    covers: list[CoverRecord]
    identity_candidates: list[IdentityEvidence]  # 按优先级排序
    chosen_identity_key: str
    chosen_identity_tier: int
    permalinks: list[str]              # 解析后的 permalink（仅作证据，不用于唯一化）
    structural_fingerprint: str        # P5 兜底指纹
    raw_fragment: str                  # 条目原始 XML 片段（页面“订阅源片段”）
    xml_base: str | None               # 条目生效的 xml:base


@dataclass
class ParsedFeed:
    """单个订阅源文档的解析结果（一次快照）。"""
    feed_url: str
    format: str                        # "RSS 2.0" / "Atom 1.0" / 未知时报错不产生对象
    feed_identity_key: str
    feed_identity_tier: int
    feed_identity_evidence: list[IdentityEvidence]
    title: str | None
    self_link: str | None
    site_link: str | None
    declared_base: str | None          # 根元素 xml:base
    links: list[LinkRecord]
    covers: list[CoverRecord]
    entries: list[ParsedEntry]
    fetch_error: str | None = None     # 抓取阶段错误（超时/404/格式错误等）
    raw_document: str = ""             # 完整原始 XML（证据）


def to_jsonable(obj: Any) -> Any:
    """把 dataclass 结构转为可 json.dumps 的普通结构。"""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj
