# -*- coding: utf-8 -*-
"""规定采集接口实现。

采集器只能通过以下公开接口取得原文，不读取测试真值目录：
1. 本地文件（.json / .csv / .txt，由样例生成器产出）；
2. HTTP 公开接口（GET，JSON/CSV/HTML，requests + BeautifulSoup 解析）；
3. 浏览器 DOM 交互（Playwright，模拟用户读取渲染后页面）。

每条原始记录带来源位置（文件行列、URL+元素选择器、表格坐标），
地区规则只接受显式声明（页面 meta / 文件字段 / 用户参数），缺省为 und。
"""
from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass

from .locales import DEFAULT_LOCALE


class CollectorError(Exception):
    """采集阶段错误（网络、文件、结构、缺依赖等），携带可读信息。"""


@dataclass
class RawRecord:
    raw_value: str
    source_type: str          # file | http_json | http_csv | http_html | browser_dom
    location: dict
    declared_locale: str = DEFAULT_LOCALE
    row_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "raw_value": self.raw_value,
            "source_type": self.source_type,
            "location": dict(self.location),
            "declared_locale": self.declared_locale,
            "row_id": self.row_id,
        }


def resolve_locale(explicit: str | None, declared: str | None) -> tuple[str, str]:
    """地区规则优先级：用户显式选择 > 源声明 > und（未定，不猜测）。"""
    if explicit:
        return explicit, "用户显式选择"
    if declared:
        return declared, "源数据显式声明"
    return DEFAULT_LOCALE, "无显式声明，按未定地区处理（歧义将报错）"


# ---------------------------------------------------------------- 本地文件

def collect_file(path: str, explicit_locale: str | None = None,
                 value_field: str = "value",
                 id_field: str = "id") -> list[RawRecord]:
    if not os.path.isfile(path):
        raise CollectorError(f"文件不存在: {path}")
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".json":
            with io.open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            return _records_from_json(payload, "file",
                                      {"path": os.path.abspath(path)},
                                      explicit_locale, value_field, id_field)
        if ext == ".csv":
            with io.open(path, "r", encoding="utf-8-sig", newline="") as f:
                return _records_from_csv(f, "file",
                                         {"path": os.path.abspath(path)},
                                         explicit_locale, value_field, id_field)
        if ext == ".txt":
            with io.open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            return _records_from_lines(lines, "file",
                                       {"path": os.path.abspath(path)},
                                       explicit_locale)
        raise CollectorError(f"不支持的文件类型 {ext}（支持 .json/.csv/.txt）")
    except CollectorError:
        raise
    except Exception as exc:  # 结构/编码错误转为可读采集错误
        raise CollectorError(f"读取文件 {path} 失败: {exc}") from exc


def _records_from_json(payload, source_type, base_loc, explicit,
                       value_field, id_field) -> list[RawRecord]:
    declared = payload.get("locale") if isinstance(payload, dict) else None
    locale, origin = resolve_locale(explicit, declared)
    rows = payload.get("rows", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise CollectorError("JSON 结构需为 {locale, rows:[...]} 或数组")
    out: list[RawRecord] = []
    for i, row in enumerate(rows):
        if value_field not in row:
            raise CollectorError(f"第 {i + 1} 行缺少字段 {value_field!r}")
        out.append(RawRecord(
            raw_value=str(row[value_field]),
            source_type=source_type,
            location={**base_loc, "index": i, "json_path": f"$.rows[{i}].{value_field}",
                      "locale_origin": origin},
            declared_locale=locale,
            row_id=str(row.get(id_field, i)),
        ))
    return out


def _records_from_csv(stream, source_type, base_loc, explicit,
                      value_field, id_field) -> list[RawRecord]:
    reader = csv.DictReader(stream)
    if reader.fieldnames is None:
        raise CollectorError("CSV 缺少表头")
    declared = None
    # 约定：表头中可用 locale=<code> 列声明地区
    locale_col = next((c for c in reader.fieldnames if c.strip().lower() == "locale"), None)
    out: list[RawRecord] = []
    for line_no, row in enumerate(reader, start=2):
        if locale_col and row.get(locale_col):
            declared = row[locale_col].strip()
        locale, origin = resolve_locale(explicit, declared)
        if value_field not in row:
            raise CollectorError(f"CSV 第 {line_no} 行缺少列 {value_field!r}")
        out.append(RawRecord(
            raw_value=row[value_field],
            source_type=source_type,
            location={**base_loc, "line": line_no, "column": value_field,
                      "locale_origin": origin},
            declared_locale=locale,
            row_id=(row.get(id_field) or str(line_no)),
        ))
    return out


def _records_from_lines(lines, source_type, base_loc, explicit) -> list[RawRecord]:
    locale, origin = resolve_locale(explicit, None)
    out: list[RawRecord] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        out.append(RawRecord(
            raw_value=line.strip(),
            source_type=source_type,
            location={**base_loc, "line": line_no, "locale_origin": origin},
            declared_locale=locale,
            row_id=str(line_no),
        ))
    return out


# ---------------------------------------------------------------- HTTP 接口

def collect_http(url: str, explicit_locale: str | None = None,
                 value_field: str = "value", id_field: str = "id",
                 timeout: float = 10.0) -> list[RawRecord]:
    try:
        import requests
    except ImportError as exc:
        raise CollectorError("缺少 requests 依赖，请先安装 requirements.txt") from exc
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:
        raise CollectorError(f"HTTP GET {url} 失败: {exc}") from exc
    ctype = resp.headers.get("Content-Type", "").lower()
    if "json" in ctype or url.endswith(".json"):
        try:
            payload = resp.json()
        except Exception as exc:
            raise CollectorError(f"响应不是合法 JSON: {exc}") from exc
        return _records_from_json(payload, "http_json", {"url": url},
                                  explicit_locale, value_field, id_field)
    if "csv" in ctype or url.endswith(".csv"):
        return _records_from_csv(io.StringIO(resp.text), "http_csv",
                                 {"url": url}, explicit_locale,
                                 value_field, id_field)
    if "html" in ctype or url.endswith(".html") or "<html" in resp.text.lower():
        return _records_from_html(resp.text, "http_html", {"url": url},
                                  explicit_locale, value_field, id_field)
    raise CollectorError(f"无法识别的响应类型: {ctype or '未知'}")


def _records_from_html(html: str, source_type: str, base_loc: dict,
                       explicit: str, value_field: str, id_field) -> list[RawRecord]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "measurement-locale"})
    declared = meta.get("content") if meta else None
    locale, origin = resolve_locale(explicit, declared)
    table = soup.find("table", id="measurements") or soup.find("table")
    if table is None:
        raise CollectorError("HTML 中未找到测量数据表 <table>")
    rows = table.find_all("tr")
    out: list[RawRecord] = []
    data_index = 0
    for tr in rows:
        cells = tr.find_all(["td", "th"])
        if not cells or all(c.name == "th" for c in cells):
            continue
        rid = tr.get("data-id") or str(data_index + 1)
        cell_texts = [c.get_text(strip=True) for c in cells]
        # 约定末列为测量值；若有 data-value 优先使用
        value_cell = cells[-1]
        raw = value_cell.get("data-value") or value_cell.get_text(strip=True)
        out.append(RawRecord(
            raw_value=raw,
            source_type=source_type,
            location={**base_loc, "table_row": data_index + 1,
                      "selector": f"table#measurements tr[data-id='{rid}'] td:last-child",
                      "row_cells": cell_texts, "locale_origin": origin},
            declared_locale=locale,
            row_id=str(rid),
        ))
        data_index += 1
    return out


# ---------------------------------------------------------------- 浏览器 DOM

def collect_browser(url: str, explicit_locale: str | None = None,
                    headless: bool = True, timeout_ms: int = 15000) -> list[RawRecord]:
    """通过 Playwright 像真实用户一样读取渲染后的 DOM。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CollectorError(
            "缺少 playwright 依赖（pip install playwright && playwright install chromium）"
        ) from exc
    out: list[RawRecord] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            page = browser.new_page()
            page.goto(url, timeout=timeout_ms, wait_until="networkidle")
            # 页面显式声明的地区
            declared = page.locator(
                "meta[name='measurement-locale']"
            ).get_attribute("content")
            locale, origin = resolve_locale(explicit_locale, declared)
            rows = page.locator("table#measurements tbody tr")
            count = rows.count()
            for i in range(count):
                row = rows.nth(i)
                rid = row.get_attribute("data-id") or str(i + 1)
                cell = row.locator("td").last
                raw = cell.get_attribute("data-value") or cell.inner_text()
                cell_texts = row.locator("td").all_inner_texts()
                out.append(RawRecord(
                    raw_value=raw.strip(),
                    source_type="browser_dom",
                    location={"url": url, "table_row": i + 1,
                              "selector": f"table#measurements tr[data-id='{rid}'] td:last-child",
                              "row_cells": [t.strip() for t in cell_texts],
                              "locale_origin": origin},
                    declared_locale=locale,
                    row_id=str(rid),
                ))
            browser.close()
    except CollectorError:
        raise
    except Exception as exc:
        raise CollectorError(f"浏览器采集 {url} 失败: {exc}") from exc
    if not out:
        raise CollectorError("浏览器未采集到任何测量行（选择器 table#measurements tbody tr）")
    return out
