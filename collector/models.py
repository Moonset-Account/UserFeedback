"""采集过程中的数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AcquiredLink:
    """一次“从页面取得地址”的结果。"""
    download_url: str
    version: str
    experiment_id: str
    evidence: dict[str, Any]  # 来源证据：URL、状态码、解析方式、DOM 片段等


@dataclass
class DownloadResponse:
    """一次下载尝试的原始结果（不做业务判断）。"""
    status_code: int
    headers: dict[str, str]
    content: bytes
    final_url: str
    transport_error: str | None = None


@dataclass
class ValidationCheck:
    name: str            # type / structure / identity
    passed: bool
    detail: str


@dataclass
class ValidationReport:
    passed: bool
    checks: list[ValidationCheck] = field(default_factory=list)
    manifest: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks],
            "manifest": self.manifest,
        }


class BackendError(RuntimeError):
    """协议后端自身错误（如浏览器未安装、页面结构无法解析）。"""


class AcquireError(BackendError):
    """无法从页面取得下载地址。"""
