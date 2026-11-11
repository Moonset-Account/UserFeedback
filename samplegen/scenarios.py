"""虚构订阅源场景定义与夹具生成。

所有机构、讲座、人物、音频均为虚构。生成结果：
  <dest>/media/   最小合法 WAV/MP3/PNG 文件
  <dest>/feeds/   RSS/Atom XML（含 xml:base、多链接关系、标识冲突等）
  <dest>/feeds/malformed.xml、unknown.xml 等异常文档

返回清单中的 expected_* 字段是独立真值，仅 tests/ 使用，
采集器运行路径从不读取这些字段。
"""
from __future__ import annotations

from pathlib import Path

from .mediafactory import write_mp3_stub, write_png, write_wav

# ---------------------------------------------------------------------------
# 场景一：正常场景（RSS + Atom，覆盖 permalink 差异、Atom id、xml:base、
# 多链接关系、同名不同实体、双附件、封面归属）
# ---------------------------------------------------------------------------

def _normal_rss(base: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>云栖大学 · 公开研究讲座</title>
    <link>{base}/site/yunqi-lectures</link>
    <description>云栖大学发布的虚构公开研究讲座订阅源（RSS 2.0）</description>
    <atom:link href="{base}/feeds/normal-rss-v1.xml" rel="self" type="application/rss+xml"/>
    <itunes:image href="{base}/media/cover-yunqi.png"/>
    <item>
      <title>量子纠错入门（上）</title>
      <link>{base}/site/talks/ytu-2024-016-page</link>
      <guid isPermaLink="true">{base}/permalink/ytu-2024-016</guid>
      <pubDate>Mon, 04 Mar 2024 09:00:00 +0800</pubDate>
      <author>讲师：闻笛（虚构人物）</author>
      <description>本讲介绍表面码与重复码的基本概念。</description>
      <enclosure url="{base}/media/ytu-2024-016.wav" length="4000" type="audio/wav"/>
      <itunes:image href="{base}/media/cover-qec-1.png"/>
    </item>
    <item>
      <title>量子纠错入门</title>
      <link>{base}/site/talks/ytu-2024-017-page</link>
      <guid isPermaLink="false">ytu-lecture-2024-017</guid>
      <pubDate>Tue, 12 Mar 2024 14:30:00 +0800</pubDate>
      <author>讲师：闻笛（虚构人物）</author>
      <description>不透明 guid（isPermaLink=false）用例，标题与另一实体相同。</description>
      <enclosure url="{base}/media/ytu-2024-017.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <title>深海观测网络的数据回传</title>
      <link>{base}/site/talks/ocean-page</link>
      <!-- permalink 与 link 不同：两者都必须保留，禁止互相覆盖 -->
      <guid isPermaLink="true">{base}/permalink/ocean-stable-022</guid>
      <pubDate>Thu, 18 Apr 2024 10:00:00 +0800</pubDate>
      <description>guid permalink 与普通 link 地址不一致的用例。</description>
      <enclosure url="{base}/media/ocean-022.wav" type="audio/wav"/>
    </item>
    <item>
      <!-- 无 guid、无 atom:id：只能落到 P5 结构指纹 -->
      <title>午间沙龙：如何阅读综述论文</title>
      <link>{base}/site/talks/salon-no-id</link>
      <pubDate>Fri, 26 Apr 2024 12:15:00 +0800</pubDate>
      <description>缺少任何显式标识的条目，验证 P5 兜底与弱标识提示。</description>
      <enclosure url="{base}/media/salon-no-id.wav" type="audio/wav"/>
    </item>
    <item>
      <!-- 与第二条标题完全相同，但 guid 不同：同名不同实体 -->
      <title>量子纠错入门</title>
      <link>{base}/site/talks/ytu-2025-117-page</link>
      <guid isPermaLink="false">ytu-lecture-2025-117</guid>
      <pubDate>Wed, 07 May 2025 15:00:00 +0800</pubDate>
      <author>讲师：岑远（虚构人物）</author>
      <description>与 2024 年同名讲座是两个实体，验证“标题不参与唯一化”。</description>
      <enclosure url="{base}/media/ytu-2025-117.mp3" type="audio/mpeg"/>
    </item>
  </channel>
</rss>
"""


def _normal_atom(base: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- 根 xml:base：所有相对地址的第一跳 -->
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:media="http://search.yahoo.com/mrss/"
      xml:base="{base}/"
      xml:lang="zh-CN">
  <title>星河研究院 · 学术讲座（Atom）</title>
  <id>urn:uuid:fictional-xinghe-feed-2024</id>
  <updated>2025-06-01T08:00:00Z</updated>
  <logo>media/cover-xinghe.png</logo>
  <link href="feeds/normal-atom.xml" rel="self" type="application/atom+xml"/>
  <link href="site/xinghe-home" rel="alternate" type="text/html"/>
  <entry>
    <title>城市热岛效应的高分辨率建模</title>
    <id>urn:uuid:fictional-entry-uhi-001</id>
    <updated>2024-02-10T09:30:00Z</updated>
    <published>2024-02-09T09:30:00Z</published>
    <author><name>研究员：麦苏（虚构人物）</name></author>
    <summary>同一 entry 上携带多种 link 关系与相对 enclosure。</summary>
    <link href="site/talks/uhi-001" rel="alternate" type="text/html"/>
    <!-- 相对 enclosure，依赖根 xml:base 解析 -->
    <link href="media/uhi-001.mp3" rel="enclosure" type="audio/mpeg" length="512"/>
    <link href="site/talks/uhi-001/related" rel="related" type="text/html"
          title="相关数据集（虚构）"/>
    <link href="site/talks/uhi-001/via" rel="via" type="text/html"/>
    <link href="site/talks/uhi-001/payment" rel="payment" type="text/html"/>
    <media:thumbnail url="media/cover-uhi.png"/>
  </entry>
  <entry xml:base="feeds/">
    <!-- 条目级 xml:base 覆盖根 base；enclosure 再回退一级目录 -->
    <title>固态电池界面的原位表征</title>
    <id>urn:uuid:fictional-entry-battery-002</id>
    <updated>2024-03-22T11:00:00Z</updated>
    <author><name>研究员：贺兰春（虚构人物）</name></author>
    <summary>条目级 xml:base + 相对 enclosure 的两级解析。</summary>
    <link href="../site/talks/battery-002" rel="alternate" type="text/html"/>
    <link xml:base="../media/" href="battery-002.wav" rel="enclosure" type="audio/wav"/>
  </entry>
  <entry>
    <title>星载 SAR 的干扰抑制</title>
    <id>urn:uuid:fictional-entry-sar-003</id>
    <updated>2024-05-19T16:20:00Z</updated>
    <summary>同一讲座的两种音频格式（双附件）。</summary>
    <link href="site/talks/sar-003" rel="alternate" type="text/html"/>
    <link href="media/sar-003.mp3" rel="enclosure" type="audio/mpeg"/>
    <link href="media/sar-003.wav" rel="enclosure" type="audio/wav"/>
  </entry>
</feed>
"""


# ---------------------------------------------------------------------------
# 场景二：边界场景（标识冲突、同名不同实体、相对 enclosure、媒体不可用）
# ---------------------------------------------------------------------------

def _edge_rss(base: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>边界实验室 · 冲突与异常用例（虚构）</title>
    <link>{base}/site/edge-lab</link>
    <description>专门用于验证身份冲突不被静默覆盖的虚构订阅源</description>
    <atom:link href="{base}/feeds/edge-conflicts.xml" rel="self" type="application/rss+xml"/>
    <item>
      <title>冲突讲座 A：重复 guid</title>
      <link>{base}/site/talks/collision-a</link>
      <guid isPermaLink="false">collision-id-001</guid>
      <pubDate>Mon, 03 Jun 2024 09:00:00 +0800</pubDate>
      <enclosure url="{base}/media/collision-a.wav" type="audio/wav"/>
    </item>
    <item>
      <!-- 与上一条声明了完全相同的 guid：文档自身标识冲突 -->
      <title>冲突讲座 B：重复 guid</title>
      <link>{base}/site/talks/collision-b</link>
      <guid isPermaLink="false">collision-id-001</guid>
      <pubDate>Tue, 04 Jun 2024 09:00:00 +0800</pubDate>
      <enclosure url="{base}/media/collision-b.wav" type="audio/wav"/>
    </item>
    <item>
      <title>标识重叠讲座 C</title>
      <link>{base}/site/talks/overlap-c-page</link>
      <!-- C 的 guid permalink 等于 D 的普通 link：跨身份线索重叠 -->
      <guid isPermaLink="true">{base}/talks/overlap-U</guid>
      <pubDate>Wed, 05 Jun 2024 09:00:00 +0800</pubDate>
      <enclosure url="{base}/media/overlap-c.wav" type="audio/wav"/>
    </item>
    <item>
      <title>标识重叠讲座 D</title>
      <link>{base}/talks/overlap-U</link>
      <guid isPermaLink="false">opaque-guid-D</guid>
      <pubDate>Thu, 06 Jun 2024 09:00:00 +0800</pubDate>
      <enclosure url="{base}/media/overlap-d.wav" type="audio/wav"/>
    </item>
    <item>
      <title>同名讲座：边缘计算</title>
      <link>{base}/site/talks/dup-title-1</link>
      <guid isPermaLink="false">edge-title-alpha</guid>
      <pubDate>Fri, 07 Jun 2024 09:00:00 +0800</pubDate>
      <enclosure url="{base}/media/dup-title-1.wav" type="audio/wav"/>
    </item>
    <item>
      <title>同名讲座：边缘计算</title>
      <link>{base}/site/talks/dup-title-2</link>
      <guid isPermaLink="false">edge-title-beta</guid>
      <pubDate>Sat, 08 Jun 2024 09:00:00 +0800</pubDate>
      <enclosure url="{base}/media/dup-title-2.wav" type="audio/wav"/>
    </item>
    <item xml:base="{base}/media/">
      <!-- 三级 xml:base 链上的相对 enclosure，文件真实存在 -->
      <title>相对地址讲座：嵌套 xml:base</title>
      <link>{base}/site/talks/relative-enc</link>
      <guid isPermaLink="false">relative-enclosure-001</guid>
      <pubDate>Sun, 09 Jun 2024 09:00:00 +0800</pubDate>
      <enclosure xml:base="nested/" url="../rel-demo.wav" type="audio/wav"/>
    </item>
    <item>
      <title>不可用媒体讲座：404 与 503</title>
      <link>{base}/site/talks/unavailable</link>
      <guid isPermaLink="false">unavailable-media-001</guid>
      <pubDate>Mon, 10 Jun 2024 09:00:00 +0800</pubDate>
      <enclosure url="{base}/error/404.mp3" type="audio/mpeg"/>
      <enclosure url="{base}/error/503.mp3" type="audio/mpeg"/>
      <itunes:image href="{base}/error/cover-missing.png"/>
    </item>
    <item>
      <title>永久下线讲座：410 Gone</title>
      <link>{base}/site/talks/gone</link>
      <guid isPermaLink="true">{base}/permalink/gone-010</guid>
      <pubDate>Tue, 11 Jun 2024 09:00:00 +0800</pubDate>
      <enclosure url="{base}/error/410.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <!-- 既无 guid 也无 link：P5 结构指纹兜底，弱标识仅在本订阅源内有效 -->
      <title>无标识讲座：结构指纹兜底</title>
      <pubDate>Wed, 12 Jun 2024 09:00:00 +0800</pubDate>
      <author>匿名交流（虚构）</author>
      <enclosure url="{base}/media/no-identity.wav" type="audio/wav"/>
    </item>
  </channel>
</rss>
"""


def _edge_atom(base: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:base="{base}/">
  <title>边界研究院（Atom）· 标识重复用例</title>
  <id>urn:uuid:fictional-edge-atom-feed</id>
  <updated>2024-06-12T00:00:00Z</updated>
  <link href="feeds/edge-atom.xml" rel="self" type="application/atom+xml"/>
  <entry>
    <title>Atom 重复标识之一</title>
    <id>urn:uuid:fictional-dup-atom-id</id>
    <updated>2024-06-12T01:00:00Z</updated>
    <link href="site/talks/atom-dup-1" rel="alternate" type="text/html"/>
    <link href="media/atom-dup-1.wav" rel="enclosure" type="audio/wav"/>
    <link href="site/talks/atom-dup-1/edit" rel="edit" type="application/atom+xml;type=entry"/>
  </entry>
  <entry>
    <!-- 与上一条 atom:id 完全相同：P1 标识冲突，两条都保留 -->
    <title>Atom 重复标识之二</title>
    <id>urn:uuid:fictional-dup-atom-id</id>
    <updated>2024-06-12T02:00:00Z</updated>
    <link href="site/talks/atom-dup-2" rel="alternate" type="text/html"/>
    <link href="error/404.mp3" rel="enclosure" type="audio/mpeg"/>
    <link href="site/talks/atom-dup-2/pay" rel="payment" type="text/html"/>
  </entry>
  <entry xml:base="feeds/deep/">
    <title>多级 base 的相对附件</title>
    <id>urn:uuid:fictional-deep-base-entry</id>
    <updated>2024-06-12T03:00:00Z</updated>
    <link xml:base="" href="../../site/talks/deep-base" rel="alternate" type="text/html"/>
    <link xml:base="../../media/" href="deep-base.wav" rel="enclosure" type="audio/wav"/>
  </entry>
</feed>
"""


# ---------------------------------------------------------------------------
# 场景三：完整验收（更正版本、媒体换址、超时、格式错误、未知协议、feed 冲突）
# ---------------------------------------------------------------------------

def _full_rss_v1(base: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>长期观测讲座 · 版本 v1（虚构）</title>
    <link>{base}/site/longrun</link>
    <atom:link href="{base}/feeds/full-rss.xml" rel="self" type="application/rss+xml"/>
    <item>
      <title>可重复讲座（初稿标题）</title>
      <link>{base}/site/talks/stable-001</link>
      <guid isPermaLink="false">stable-guid-001</guid>
      <pubDate>Thu, 01 Aug 2024 09:00:00 +0800</pubDate>
      <description>v1：媒体地址为 media-v1，后续版本将更换地址但身份不变。</description>
      <enclosure url="{base}/media/stable-001-v1.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <title>更正版本讲座（含错误的初稿）</title>
      <link>{base}/site/talks/correction-009</link>
      <guid isPermaLink="false">correction-guid-009</guid>
      <pubDate>Fri, 02 Aug 2024 09:00:00 +0800</pubDate>
      <description>v1 初稿：时间标注有误，将在 v2 更正。</description>
      <enclosure url="{base}/media/correction-009-v1.wav" type="audio/wav"/>
    </item>
  </channel>
</rss>
"""


def _full_rss_v2(base: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>长期观测讲座 · 版本 v2（虚构）</title>
    <link>{base}/site/longrun</link>
    <!-- self 链接与 v1 相同：feed 身份不变，条目按同一订阅源归并 -->
    <atom:link href="{base}/feeds/full-rss.xml" rel="self" type="application/rss+xml"/>
    <item>
      <!-- guid 不变，仅媒体地址更换：验证“媒体地址更换但条目不变” -->
      <title>可重复讲座（定稿标题）</title>
      <link>{base}/site/talks/stable-001</link>
      <guid isPermaLink="false">stable-guid-001</guid>
      <pubDate>Thu, 01 Aug 2024 09:00:00 +0800</pubDate>
      <description>v2：附件迁移到新地址，标题与描述同时修订。</description>
      <enclosure url="{base}/media/stable-001-v2.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <!-- 同一 guid 的更正版本：归并为同一实体的两个 revision -->
      <title>更正版本讲座（已勘误）</title>
      <link>{base}/site/talks/correction-009</link>
      <guid isPermaLink="false">correction-guid-009</guid>
      <pubDate>Sat, 03 Aug 2024 09:00:00 +0800</pubDate>
      <description>v2 勘误：更正活动时间，附件地址保持不变。</description>
      <enclosure url="{base}/media/correction-009-v1.wav" type="audio/wav"/>
    </item>
    <item>
      <title>v2 新增讲座</title>
      <link>{base}/site/talks/new-in-v2</link>
      <guid isPermaLink="false">new-guid-v2-100</guid>
      <pubDate>Sun, 04 Aug 2024 09:00:00 +0800</pubDate>
      <enclosure url="{base}/media/new-in-v2.wav" type="audio/wav"/>
    </item>
  </channel>
</rss>
"""


_MALFORMED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>未闭合标签的非法文档（虚构）</title>
    <item>
      <title>这个 item 没有闭合
</rss>
"""

_UNKNOWN_ROOT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!-- RSS 1.0 (RDF) 不在本采集器声明支持的协议子集内 -->
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/">
  <channel rdf:about="http://example.invalid/fictional">
    <title>虚构 RSS 1.0 源</title>
  </channel>
</rdf:RDF>
"""


def _atom_feed_conflict_a(base: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>镜像 Alpha（虚构）</title>
  <!-- 两个文档 atom:id 不同，却声明了相同 self 链接：feed 身份冲突 -->
  <id>urn:uuid:fictional-mirror-alpha-id</id>
  <updated>2024-09-01T00:00:00Z</updated>
  <link href="{base}/feeds/mirror-shared.xml" rel="self" type="application/atom+xml"/>
  <entry>
    <title>镜像 Alpha 的讲座</title>
    <id>urn:uuid:fictional-mirror-alpha-entry</id>
    <updated>2024-09-01T00:00:00Z</updated>
    <link href="{base}/media/mirror-alpha.wav" rel="enclosure" type="audio/wav"/>
  </entry>
</feed>
"""


def _atom_feed_conflict_b(base: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>镜像 Beta（虚构）</title>
  <id>urn:uuid:fictional-mirror-beta-id</id>
  <updated>2024-09-02T00:00:00Z</updated>
  <link href="{base}/feeds/mirror-shared.xml" rel="self" type="application/atom+xml"/>
  <entry>
    <title>镜像 Beta 的讲座</title>
    <id>urn:uuid:fictional-mirror-beta-entry</id>
    <updated>2024-09-02T00:00:00Z</updated>
    <link href="{base}/media/mirror-beta.wav" rel="enclosure" type="audio/wav"/>
  </entry>
</feed>
"""


def generate_fixtures(dest: str | Path, base_url: str) -> dict:
    """在 dest 下生成全部虚构夹具，返回场景清单（含仅供测试的真值）。"""
    dest = Path(dest)
    media_dir = dest / "media"
    feeds_dir = dest / "feeds"
    media_dir.mkdir(parents=True, exist_ok=True)
    feeds_dir.mkdir(parents=True, exist_ok=True)

    media_files = [
        "ytu-2024-016.wav", "ocean-022.wav", "salon-no-id.wav",
        "collision-a.wav", "collision-b.wav", "overlap-c.wav",
        "overlap-d.wav", "dup-title-1.wav", "dup-title-2.wav",
        "rel-demo.wav", "atom-dup-1.wav", "deep-base.wav",
        "battery-002.wav", "sar-003.wav", "no-identity.wav",
        "correction-009-v1.wav", "new-in-v2.wav",
        "mirror-alpha.wav", "mirror-beta.wav",
    ]
    for name in media_files:
        write_wav(media_dir / name)
    for name in ["ytu-2024-017.mp3", "ytu-2025-117.mp3", "uhi-001.mp3",
                 "sar-003.mp3", "stable-001-v1.mp3", "stable-001-v2.mp3"]:
        write_mp3_stub(media_dir / name)
    for name in ["cover-yunqi.png", "cover-qec-1.png", "cover-xinghe.png",
                 "cover-uhi.png"]:
        write_png(media_dir / name)
    (media_dir / "nested").mkdir(exist_ok=True)

    documents = {
        "normal-rss-v1.xml": _normal_rss(base_url),
        "normal-atom.xml": _normal_atom(base_url),
        "edge-conflicts.xml": _edge_rss(base_url),
        "edge-atom.xml": _edge_atom(base_url),
        "full-rss-v1.xml": _full_rss_v1(base_url),
        "full-rss-v2.xml": _full_rss_v2(base_url),
        "malformed.xml": _MALFORMED_XML,
        "unknown.xml": _UNKNOWN_ROOT_XML,
        "mirror-alpha.xml": _atom_feed_conflict_a(base_url),
        "mirror-beta.xml": _atom_feed_conflict_b(base_url),
    }
    for name, content in documents.items():
        (feeds_dir / name).write_text(content, encoding="utf-8")

    manifest = {
        "source_root": str(dest),
        "base_url": base_url,
        "scenarios": {
            "demo-normal": {
                "label": "示例一：正常场景（RSS+Atom）",
                "sources": [
                    {"url": f"{base_url}/feeds/normal-rss-v1.xml", "version_label": "RSS"},
                    {"url": f"{base_url}/feeds/normal-atom.xml", "version_label": "Atom"},
                ],
                "expected": {
                    "feed_count": 2,
                    "catalog_entity_count": 8,       # RSS 5 + Atom 3
                    "all_enclosures_available": True,
                    "duplicate_title_pairs": 1,      # 两个“量子纠错入门”
                    "permalink_mismatch_entry_guid": "guid::"
                        f"{base_url}/permalink/ocean-stable-022",
                    "relative_enclosure_resolves_to":
                        f"{base_url}/media/rel-demo.wav",
                    # 午间沙龙无 guid 但有 link → P3 permalink；P5 指纹仍作为候选保留
                    "permalink_fallback_entries": 1,
                    "atom_multi_relation_entry_id":
                        "atom-id::urn:uuid:fictional-entry-uhi-001",
                },
            },
            "demo-edge": {
                "label": "示例二：边界场景（冲突/相对地址/媒体不可用）",
                "sources": [
                    {"url": f"{base_url}/feeds/edge-conflicts.xml", "version_label": "RSS-冲突"},
                    {"url": f"{base_url}/feeds/edge-atom.xml", "version_label": "Atom-冲突"},
                ],
                "expected": {
                    "feed_count": 2,
                    "duplicate_identity_conflicts": 2,   # RSS guid 碰撞 + Atom id 碰撞
                    "identifier_overlap_conflicts": 1,
                    "duplicate_title_warnings": 1,
                    "unavailable_enclosure_urls": [
                        f"{base_url}/error/404.mp3",
                        f"{base_url}/error/503.mp3",
                        f"{base_url}/error/410.mp3",
                    ],
                    "unavailable_status_codes": [404, 410, 503],
                    "relative_enclosure_resolves_to":
                        f"{base_url}/media/rel-demo.wav",
                    "collision_identity_key": "guid::collision-id-001",
                    "no_silent_overwrite": True,
                },
            },
            "full-acceptance": {
                "label": "完整验收：更正版本/媒体换址/超时/非法文档/feed 冲突",
                "sources": [
                    {"url": f"{base_url}/feeds/full-rss-v1.xml", "version_label": "长期源-v1"},
                    {"url": f"{base_url}/feeds/full-rss-v2.xml", "version_label": "长期源-v2"},
                    {"url": f"{base_url}/error/slow.xml", "version_label": "超时源"},
                    {"url": f"{base_url}/feeds/malformed.xml", "version_label": "非法XML"},
                    {"url": f"{base_url}/feeds/unknown.xml", "version_label": "未知协议"},
                    {"url": f"{base_url}/error/missing-feed.xml", "version_label": "404源"},
                    {"url": f"{base_url}/feeds/mirror-alpha.xml", "version_label": "镜像Alpha"},
                    {"url": f"{base_url}/feeds/mirror-beta.xml", "version_label": "镜像Beta"},
                ],
                "expected": {
                    "parsed_feed_count": 4,           # v1 v2 alpha beta
                    "fetch_or_parse_error_count": 4,  # slow/malformed/unknown/missing
                    "stable_guid": "guid::stable-guid-001",
                    "stable_entity_revision_count": 2,
                    "stable_entity_media_change_count": 1,
                    "correction_guid": "guid::correction-guid-009",
                    "correction_revision_count": 2,
                    "cross_feed_identifier_conflicts": 1,
                    "v1_media_url": f"{base_url}/media/stable-001-v1.mp3",
                    "v2_media_url": f"{base_url}/media/stable-001-v2.mp3",
                },
                "timeout": 2.0,
            },
        },
    }
    return manifest
