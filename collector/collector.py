"""采集器：只经规定的公开接口、文件或浏览器交互读取步骤数据。

三条公开采集通道：
1. collect_from_file：读取 rpa-order/v1 JSON 文件（禁止读取 truth/ 真值目录）；
2. collect_via_http：GET 本地源站公开接口 /api/scenario/<id>（标准库 urllib，
   不依赖业务服务）；
3. collect_dom_order：通过 Playwright 打开本地流程编辑页，从 DOM 公开结构
   （#step-list li[data-step-id]）回读当前卡片顺序。
另有 collect_dom_via_bs4：对源站返回的 HTML 用 BeautifulSoup 解析静态顺序，
声明 HTML 子集：仅识别 id="step-list" 下带 data-step-id 的 <li>。

采集器不包含任何规划逻辑，也绝不读取 samples/truth 目录。
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from bs4 import BeautifulSoup

from core.models import Spec
from core.parser import ProtocolError, parse_obj, parse_text

TRUTH_DIR_NAME = "truth"


class CollectionError(RuntimeError):
    """采集阶段错误（通道不可用、数据非法等）。"""


# --------------------------------------------------------------------------- #
# 通道 1：文件
# --------------------------------------------------------------------------- #
def collect_from_file(path: str | Path) -> Spec:
    p = Path(path).resolve()
    if TRUTH_DIR_NAME in p.parts:
        raise CollectionError(
            "拒绝采集：truth 目录是独立测试真值，采集器不得直接读取。"
        )
    if not p.is_file():
        raise CollectionError(f"采集失败：文件不存在：{p}")
    try:
        return parse_text(p.read_text(encoding="utf-8"))
    except ProtocolError as exc:
        raise CollectionError(f"采集到的数据不符合协议：{exc}") from exc


# --------------------------------------------------------------------------- #
# 通道 2：本地源站公开 HTTP 接口
# --------------------------------------------------------------------------- #
def collect_via_http(base_url: str, scenario_id: str, timeout: float = 5.0) -> Spec:
    url = f"{base_url.rstrip('/')}/api/scenario/{scenario_id}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as exc:  # urllib 异常族较散，统一收口
        raise CollectionError(f"HTTP 采集失败（{url}）：{exc}") from exc
    try:
        return parse_obj(json.loads(raw))
    except (ProtocolError, json.JSONDecodeError) as exc:
        raise CollectionError(f"HTTP 采集到的数据不符合协议：{exc}") from exc


def collect_dom_via_bs4(base_url: str, scenario_id: str, timeout: float = 5.0) -> list[str]:
    """HTML 子集解析：仅识别 #step-list > li[data-step-id] 的静态顺序。"""
    url = f"{base_url.rstrip('/')}/source/{scenario_id}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            html = resp.read().decode("utf-8")
    except Exception as exc:
        raise CollectionError(f"HTML 采集失败（{url}）：{exc}") from exc
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find(id="step-list")
    if node is None:
        raise CollectionError("HTML 中未找到 #step-list，超出声明的 HTML 子集。")
    order = [li.get("data-step-id") for li in node.find_all("li", attrs={"data-step-id": True})]
    if not order:
        raise CollectionError("#step-list 下没有带 data-step-id 的 <li> 卡片。")
    return order


# --------------------------------------------------------------------------- #
# 通道 3：浏览器 DOM 实时回读
# --------------------------------------------------------------------------- #
DOM_READ_JS = """
() => Array.from(document.querySelectorAll('#step-list li[data-step-id]'))
           .map(li => li.getAttribute('data-step-id'))
"""


def collect_dom_order(page) -> list[str]:
    """传入 Playwright Page，回读流程编辑页当前真实顺序。"""
    order = page.evaluate(DOM_READ_JS)
    if not isinstance(order, list) or not order:
        raise CollectionError("DOM 回读为空或结构异常，拒绝继续规划。")
    return order
