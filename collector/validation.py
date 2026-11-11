"""下载内容三层校验：类型 → 结构 → 业务身份。

全部判断只依赖下载内容本身与页面声明的业务身份，不读取任何生成器真值。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile

from collector.models import ValidationCheck, ValidationReport
from generator.payloads import ZIP_EMPTY_MAGIC, ZIP_MAGIC

REQUIRED_MEMBERS = ("manifest.json", "readings.csv", "notes/README.txt")


def check_content_type(content: bytes, headers: dict[str, str]) -> ValidationCheck:
    """第一层：类型校验。

    识别两类伪装：
      1. 声明是 zip、实际是 HTML（HTTP 200 错误页）；
      2. 魔数不是 ZIP（PK 开头）的其他内容。
    """
    declared = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    head = content[:512].lstrip()
    looks_html = b"<html" in head[:200].lower() or b"<!doctype html" in head[:200].lower()
    is_zip = content[:4] in (ZIP_MAGIC, ZIP_EMPTY_MAGIC)

    if looks_html:
        return ValidationCheck(
            "type", False,
            f"内容实际为 HTML 错误页（Content-Type 声明: {declared or '未声明'}），不是导出包",
        )
    if not is_zip:
        return ValidationCheck(
            "type", False,
            f"魔数不是 ZIP（前 4 字节: {content[:4]!r}；Content-Type: {declared or '未声明'}）",
        )
    if "zip" not in declared and "octet-stream" not in declared and declared:
        return ValidationCheck("type", False, f"魔数为 ZIP 但响应头声明类型异常: {declared}")
    return ValidationCheck("type", True, f"ZIP 魔数与内容类型一致（Content-Type: {declared}）")


def check_structure(content: bytes) -> tuple[ValidationCheck, dict | None]:
    """第二层：结构校验。ZIP 完整性、必需成员、manifest 可解析、校验和匹配、CSV 可解析。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        return ValidationCheck("structure", False, f"ZIP 无法打开: {exc}"), None

    bad_member = zf.testzip()
    if bad_member is not None:
        return ValidationCheck("structure", False, f"ZIP 成员 CRC 校验失败: {bad_member}"), None

    names = set(zf.namelist())
    missing = [m for m in REQUIRED_MEMBERS if m not in names]
    if missing:
        return ValidationCheck("structure", False, f"缺少必需成员: {', '.join(missing)}"), None

    try:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return ValidationCheck("structure", False, f"manifest.json 无法解析: {exc}"), None

    # 校验和
    for member, meta in manifest.get("files", {}).items():
        if member not in names:
            return ValidationCheck("structure", False, f"manifest 声明的成员不存在: {member}"), manifest
        actual = hashlib.sha256(zf.read(member)).hexdigest()
        if actual != meta.get("sha256"):
            return ValidationCheck(
                "structure", False,
                f"{member} 校验和不匹配（manifest={str(meta.get('sha256'))[:12]}… 实际={actual[:12]}…）",
            ), manifest

    # CSV 结构
    try:
        with zf.open("readings.csv") as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8")
            rows = list(csv.reader(text))
        header = rows[0] if rows else []
        expected_header = ["sample_id", "experiment_id", "channel", "value", "captured_at"]
        if header != expected_header:
            return ValidationCheck("structure", False, f"readings.csv 表头不符: {header}"), manifest
        if len(rows) < 2:
            return ValidationCheck("structure", False, "readings.csv 没有数据行"), manifest
    except (UnicodeDecodeError, csv.Error, IndexError) as exc:
        return ValidationCheck("structure", False, f"readings.csv 解析失败: {exc}"), manifest

    return ValidationCheck(
        "structure", True,
        f"ZIP 完整；成员 {sorted(names)} 齐全；manifest 与 {len(manifest.get('files', {}))} 个校验和匹配；"
        f"readings.csv 含 {len(rows) - 1} 行数据",
    ), manifest


def check_identity(manifest: dict, expected_experiment_id: str, expected_version: str) -> ValidationCheck:
    """第三层：业务身份校验。实验编号与稳定版本号必须与页面声明一致。"""
    actual_id = manifest.get("experiment_id")
    actual_version = manifest.get("version")
    problems = []
    if actual_id != expected_experiment_id:
        problems.append(f"实验编号不符（页面={expected_experiment_id}，包内={actual_id}）")
    if actual_version != expected_version:
        problems.append(f"版本号不符（页面={expected_version}，包内={actual_version}）")
    if problems:
        return ValidationCheck("identity", False, "；".join(problems))
    return ValidationCheck(
        "identity", True,
        f"业务身份一致：experiment_id={actual_id}，version={actual_version}",
    )


def validate(content: bytes, headers: dict[str, str],
             expected_experiment_id: str, expected_version: str) -> ValidationReport:
    report = ValidationReport(passed=False)

    type_check = check_content_type(content, headers)
    report.checks.append(type_check)
    if not type_check.passed:
        return report  # 后续层无意义，短路返回

    struct_check, manifest = check_structure(content)
    report.checks.append(struct_check)
    report.manifest = manifest
    if not struct_check.passed:
        return report

    identity_check = check_identity(manifest or {}, expected_experiment_id, expected_version)
    report.checks.append(identity_check)

    report.passed = all(c.passed for c in report.checks)
    return report
