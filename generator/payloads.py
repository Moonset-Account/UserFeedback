"""虚构实验导出包（ZIP）构造与自描述校验元数据。

导出包布局（公开声明的结构约定，采集器按此做结构校验）：
  manifest.json        业务身份与校验和
  readings.csv         虚构测量数据
  notes/README.txt     说明文本

manifest.json 内含 sha256，采集器可仅凭下载内容完成类型/结构/业务身份三层校验，
无需访问任何“真值”文件。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone


def _csv_bytes(experiment_id: str, rows: int) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["sample_id", "experiment_id", "channel", "value", "captured_at"])
    # 确定性伪数据（不使用随机种子外的任何真实数据）
    for i in range(rows):
        value = round(0.01 * ((i * 37 + 11) % 1000), 4)
        writer.writerow([
            f"S{i + 1:04d}",
            experiment_id,
            "A" if i % 2 == 0 else "B",
            value,
            "2026-11-01T08:00:00Z",
        ])
    return buf.getvalue().encode("utf-8")


def _readme_bytes(experiment_id: str, version: str) -> bytes:
    return (
        "虚构实验数据（FICTIONAL DATA）\n"
        f"实验编号: {experiment_id}\n"
        f"实验版本: {version}\n"
        "本文件由本地生成器构造，仅用于采集器演练，不包含任何真实业务数据。\n"
    ).encode("utf-8")


def build_payload(
    *,
    experiment_id: str,
    version: str,
    identity_id: str | None = None,
    rows: int = 24,
) -> bytes:
    """构造导出 ZIP。

    identity_id 与 experiment_id 不同时，manifest 内写入另一个实验编号，
    用于构造“业务身份不符”场景（采集器必须识别并拒绝）。
    """
    declared_id = identity_id or experiment_id
    csv_blob = _csv_bytes(declared_id, rows)
    readme_blob = _readme_bytes(declared_id, version)

    manifest = {
        "experiment_id": declared_id,
        "version": version,
        "generated_at": datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc).isoformat(),
        "fictional": True,
        "files": {
            "readings.csv": {"sha256": hashlib.sha256(csv_blob).hexdigest()},
            "notes/README.txt": {"sha256": hashlib.sha256(readme_blob).hexdigest()},
        },
    }
    manifest_blob = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", manifest_blob)
        zf.writestr("readings.csv", csv_blob)
        zf.writestr("notes/README.txt", readme_blob)
    return out.getvalue()


# ZIP 本地文件头魔数（PK\x03\x04）；空 ZIP 为 PK\x05\x06
ZIP_MAGIC = b"PK\x03\x04"
ZIP_EMPTY_MAGIC = b"PK\x05\x06"
