"""独立预期真值（测试专属）。

这里的预期结果全部按协议语义手工推导，与样例生成器、采集引擎的内部实现互不引用；
生产代码（rip_collector / sample_generator / web）的任何模块都不允许 import 本文件。
"""
from __future__ import annotations

# demo_normal：乱序插入按 (index, seq) 收敛；子先于父重放；错误恢复；重复 seq 去重
DEMO_NORMAL_EXPECTED = {
    "status": "complete",
    "complete": True,
    "tree": {
        "id": "P-1001",
        "title": "虚构材料：纳米多孔电极在低温储能中的行为研究",
        "authors": [
            {"name": "林知微（虚构）", "orcid": "0000-0000-0000-0001"},
            {"name": "沈既明（虚构）", "orcid": "0000-0000-0000-0002"},
        ],
        "references": [
            {"id": "REF-1", "title": "虚构专著：电化学界面原理"},
            {"id": "REF-2", "title": "虚构综述：多孔电极二十年"},
        ],
        "metrics": {
            "downloads": 342,
            "keywords": ["低温储能", "纳米多孔"],
            "views": 1280,  # seq=3 挂起，seq=5 创建 /metrics 后重放
        },
        "funding": {"agency": "虚构自然科学基金", "grant": "VCG-2026-0001"},
    },
    "errors": [],
    "unresolved": [],
    "rejected_reasons": ["duplicate_seq"],
}

# demo_edge：无 end 且连接中断；peer_review 永远挂起；暂存树不得完整
DEMO_EDGE_EXPECTED = {
    "status": "interrupted",
    "complete": False,
    "pending_paths": ["/peer_review/verdict"],
    "funding_is_value": True,  # seq=8 的恢复发生在中断之前
    "references_len": 2,
}

# out_of_order：2/0/3/1 乱序插入后必须是甲乙丙丁，重复 REF-B 去重
OUT_OF_ORDER_EXPECTED = {
    "status": "complete",
    "references_ids": ["REF-B", "REF-D", "REF-A", "REF-C"],
    "references_titles": ["虚构引文乙", "虚构引文丁", "虚构引文甲", "虚构引文丙"],
    "tags": ["开放获取（虚构）", "预印本（虚构）"],
    "rejected_reasons": ["duplicate_item"],
}

# duplicates：seq=1 三次只生效首次；REF-X 两次只保留先到者
DUPLICATES_EXPECTED = {
    "status": "complete",
    "metrics_views": 100,
    "references": [
        {"id": "REF-X", "title": "虚构引文 X"},
        {"id": "REF-Y", "title": "虚构引文 Y"},
    ],
    "rejected_reasons": ["duplicate_seq", "duplicate_item"],
}

# field_errors：funding 恢复；license 保留最新错误来源 seq=5
FIELD_ERRORS_EXPECTED = {
    "status": "complete",
    "funding": {"agency": "虚构科学院", "grant": "VCG-2026-0009"},
    "license_error": {
        "code": "FORBIDDEN",
        "source_seq": 5,
        "marker": "__error__",
    },
    "authors_len": 1,
}

# illegal_paths：5 条非法消息全部拒绝且留证，合法消息照常生效
ILLEGAL_PATHS_EXPECTED = {
    "status": "complete",
    "metrics_views": 7,
    "references": [{"id": "REF-OK", "title": "合法引文（虚构）"}],
    "rejected_reasons": [
        "bad_shape",       # path 不以 / 开头（解析层）
        "out_of_range",    # patch 数组越界
        "not_a_list",      # 对对象执行 insert
        "bad_index",       # 下标 '-' 不是数字
        "bad_shape",       # error 空路径（解析层）
    ],
}

# comprehensive：坏 JSON + 非法路径 + 同 seq 重复插入 + 两条永久挂起 -> ended_unresolved
COMPREHENSIVE_EXPECTED = {
    "status": "ended_unresolved",
    "complete": False,
    "references_ids": ["REF-1", "REF-2"],
    # 第二条 REF-1 与第一条同 seq，先命中 seq 级去重
    "rejected_reasons": ["duplicate_seq", "bad_json", "bad_shape"],
    "pending_paths": ["/dataset/doi", "/peer_review/decision"],
    "license_error_seq": 4,
}
