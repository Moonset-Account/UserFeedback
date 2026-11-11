"""构建映射加载与帧解析。

映射条目按 (module_build_id, module, address) 定位，给出该地址的完整
内联展开链（inline_depth 由外向内，0 为非内联归属函数）。解析时与报告自带的
候选符号 / 内联层次交叉验证：
  - 查不到任何映射支撑           -> MISSING_MAPPING
  - 存在多个互不一致的稳定归属   -> AMBIGUOUS_MAPPING
"""

from __future__ import annotations

from typing import Any

from .models import StableAttribution


class MappingError(Exception):
    """映射文件结构错误。"""


def normalize_address(value: Any) -> str | None:
    """把地址归一化为小写 0x 十六进制字符串；无效输入返回 None。"""
    if value is None:
        return None
    if isinstance(value, int):
        if value < 0:
            return None
        return f"0x{value:x}"
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    if not text.startswith("0x"):
        text = "0x" + text
    hex_part = text[2:]
    if not hex_part or any(c not in "0123456789abcdef" for c in hex_part):
        return None
    # 去掉前导零（保留至少一位），避免不同报告的书写宽度差异
    return "0x" + hex_part.lstrip("0").rjust(1, "0")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MappingError(message)


class BuildMappingIndex:
    """构建映射的只读索引。"""

    def __init__(self, mapping_doc: dict[str, Any]):
        _require(isinstance(mapping_doc, dict), "构建映射根节点必须是对象")
        builds = mapping_doc.get("builds")
        _require(isinstance(builds, list), "构建映射缺少 builds 数组")

        # key: (build_id, module, normalized_address) -> list[entry]
        self._entries: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        self._revisions: dict[str, str] = {}
        self._known_builds: set[str] = set()

        for build in builds:
            _require(isinstance(build, dict), "builds 中存在非对象元素")
            build_id = build.get("module_build_id")
            _require(isinstance(build_id, str) and build_id, "缺少 module_build_id")
            self._known_builds.add(build_id)
            revision = build.get("source_revision")
            _require(isinstance(revision, str) and revision, f"构建 {build_id} 缺少 source_revision")
            self._revisions[build_id] = revision

            modules = build.get("modules", [])
            _require(isinstance(modules, list), f"构建 {build_id} 的 modules 必须是数组")
            for module in modules:
                _require(isinstance(module, dict), f"构建 {build_id} 中存在非对象模块")
                module_name = module.get("module")
                _require(isinstance(module_name, str) and module_name,
                         f"构建 {build_id} 中模块缺少名称")
                entries = module.get("frames", [])
                _require(isinstance(entries, list),
                         f"构建 {build_id} 模块 {module_name} 的 frames 必须是数组")
                for entry in entries:
                    self._index_entry(build_id, module_name, entry)

    def _index_entry(self, build_id: str, module_name: str, entry: dict[str, Any]) -> None:
        _require(isinstance(entry, dict), f"构建 {build_id} 中存在非对象映射条目")
        address = normalize_address(entry.get("address"))
        _require(address is not None,
                 f"构建 {build_id} 模块 {module_name} 存在无效地址条目")
        chain = entry.get("inline_chain")
        _require(isinstance(chain, list) and chain,
                 f"地址 {address} 缺少 inline_chain 数组")

        depths: list[int] = []
        for frame in chain:
            _require(isinstance(frame, dict), f"地址 {address} 的链中存在非对象帧")
            depth = frame.get("inline_depth")
            _require(isinstance(depth, int) and depth >= 0,
                     f"地址 {address} 的 inline_depth 非法")
            depths.append(depth)
            _require(isinstance(frame.get("symbol"), str) and frame["symbol"],
                     f"地址 {address} 的帧缺少 symbol")
            loc = frame.get("source")
            _require(isinstance(loc, dict), f"地址 {address} 的帧缺少 source 对象")
            _require(isinstance(loc.get("file"), str) and loc["file"],
                     f"地址 {address} 的帧缺少 source.file")
            _require(isinstance(loc.get("line"), int) and loc["line"] > 0,
                     f"地址 {address} 的帧缺少合法 source.line")
        _require(sorted(depths) == list(range(len(depths))),
                 f"地址 {address} 的 inline_depth 必须是从 0 开始的连续整数")

        self._entries.setdefault((build_id, module_name, address), []).append(entry)

    # ------------------------------------------------------------------
    def is_known_build(self, build_id: str) -> bool:
        return build_id in self._known_builds

    def source_revision(self, build_id: str) -> str | None:
        return self._revisions.get(build_id)

    def lookup_entries(
        self, build_id: str, module: str, raw_address: str
    ) -> list[dict[str, Any]] | None:
        """返回匹配的映射条目列表；构建/模块/地址完全无记录时返回 None。"""
        address = normalize_address(raw_address)
        if address is None:
            return None
        entries = self._entries.get((build_id, module, address))
        if not entries:
            return None
        return entries


def attribution_of(entry: dict[str, Any]) -> StableAttribution:
    """从一条映射条目提取稳定源码归属。

    - 最内层帧（inline_depth 最大）决定源码文件与行号；
    - 非内联帧（inline_depth=0）决定归属符号。
    """
    chain = sorted(entry["inline_chain"], key=lambda f: f["inline_depth"])
    innermost = max(chain, key=lambda f: f["inline_depth"])
    root = next(f for f in chain if f["inline_depth"] == 0)
    return StableAttribution(
        source_file=innermost["source"]["file"],
        owning_symbol=root["symbol"],
        source_line=int(innermost["source"]["line"]),
    )


def serialize_chain(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """把映射链按内联层次（外 -> 内）序列化，供输出复核。"""
    return [
        {
            "inline_depth": frame["inline_depth"],
            "symbol": frame["symbol"],
            "source_file": frame["source"]["file"],
            "source_line": frame["source"]["line"],
        }
        for frame in sorted(entry["inline_chain"], key=lambda f: f["inline_depth"])
    ]
