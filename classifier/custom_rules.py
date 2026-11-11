"""用户补充规则。

规则文件为 JSON 数组，每条规则形如：
{
  "code": "rule-banner-001",
  "name": "维护横幅补充规则",
  "phrase": "升级公告",                 // 正则（忽略大小写），对页面可见全文匹配
  "region_contains": "maintenance",     // 可选；要求命中元素所在区域 id/class 含该 token
  "category": "MAINTENANCE",            // 目标类别
  "weight": 0.5                         // -0.8 ~ 0.8
}

规则只产生带 layer="rule" 的证据，与内置信号一同参与打分；
规则非法时抛 RuleError，任务不会静默使用坏规则。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .features import PageFeatures, _region_of
from .models import CATEGORIES, Evidence
from bs4 import BeautifulSoup

MAX_ABS_WEIGHT = 0.8


class RuleError(ValueError):
    pass


def load_rules(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuleError(f"规则文件不是合法 JSON：{exc}") from exc
    if not isinstance(data, list):
        raise RuleError("规则文件顶层必须是数组")
    validated = []
    for i, raw in enumerate(data):
        validated.append(_validate(raw, i))
    return validated


def _validate(raw: dict, index: int) -> dict:
    prefix = f"第 {index + 1} 条规则"
    if not isinstance(raw, dict):
        raise RuleError(f"{prefix}不是对象")
    code = str(raw.get("code") or f"rule-{index + 1:03d}")
    phrase = raw.get("phrase")
    if not phrase or not isinstance(phrase, str):
        raise RuleError(f"{prefix}（{code}）缺少 phrase")
    category = raw.get("category")
    if category not in CATEGORIES or category in ("FETCH_FAILED", "UNKNOWN"):
        raise RuleError(
            f"{prefix}（{code}）category 必须是语义类别之一，不能为 {category}"
        )
    weight = raw.get("weight", 0.5)
    if not isinstance(weight, (int, float)) or not -MAX_ABS_WEIGHT <= weight <= MAX_ABS_WEIGHT:
        raise RuleError(f"{prefix}（{code}）weight 必须在 ±{MAX_ABS_WEIGHT} 之间")
    region = raw.get("region_contains")
    if region is not None and not isinstance(region, str):
        raise RuleError(f"{prefix}（{code}）region_contains 必须是字符串")
    try:
        pattern = re.compile(phrase, re.IGNORECASE)
    except re.error as exc:
        raise RuleError(f"{prefix}（{code}）phrase 正则非法：{exc}") from exc
    return {
        "code": code,
        "name": str(raw.get("name") or code),
        "pattern": pattern,
        "region_contains": region.lower() if region else "",
        "category": category,
        "weight": float(weight),
    }


def apply_custom_rules(
    html: str, features: PageFeatures, rules: list[dict]
) -> list[Evidence]:
    """对原始 HTML 重新做轻量匹配，返回规则证据。"""
    if not rules:
        return []
    soup = BeautifulSoup(html or "", "html.parser")
    evidences: list[Evidence] = []
    for rule in rules:
        matched_node = None
        for node in soup.find_all(string=rule["pattern"]):
            if node.parent is None:
                continue
            if rule["region_contains"]:
                # 直接沿祖先链做 token 子串匹配，不依赖内置区域分类器，
                # 用户规则可以指向任意自定义 class/id
                region = _region_of(node.parent)
                chain_ok = rule["region_contains"] in region
                if not chain_ok:
                    probe = node.parent
                    hops = 0
                    while probe is not None and hops < 12 and getattr(probe, "name", None):
                        nid = (probe.get("id") or "").lower()
                        classes = " ".join(probe.get("class") or []).lower()
                        if rule["region_contains"] in (nid or classes):
                            chain_ok = True
                            region = nid or classes
                            break
                        probe = probe.parent
                        hops += 1
                if not chain_ok:
                    continue
            matched_node = node
            break
        if matched_node is None:
            continue
        region = _region_of(matched_node.parent)
        text = re.sub(r"\s+", " ", str(matched_node)).strip()
        evidences.append(
            Evidence(
                code=rule["code"],
                layer="rule",
                message=(
                    f"补充规则「{rule['name']}」命中"
                    + (f"（区域 {region}）" if rule["region_contains"] else "（全文匹配）")
                ),
                weight=rule["weight"],
                supports=rule["category"],
                excerpt=text[:160],
                region=region,
            )
        )
    return evidences
