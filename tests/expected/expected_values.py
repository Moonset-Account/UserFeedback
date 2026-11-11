"""独立预期真值（独立字面量维护，不 import 生成器、不读取生成器内部状态）。

采集器只能从 HTTP 公开接口或 samples/*.mime 文件取得原始响应；
本文件仅供测试断言使用，采集器代码路径绝不读取这里的任何内容。
数值为虚构仪器 FICT-LAB-DEV-* 的确定性虚构测量值。
"""
from __future__ import annotations

# 每个场景的预期结论
EXPECTED_STATUS = {
    "complete": "complete",
    "fault": "fault",
    "missing_reference": "rejected",
    "duplicate_cid": "rejected",
    "parts_reordered": "complete",
    "digest_mismatch": "rejected",
}

EXPECTED_ERROR_CODE = {
    "fault": "SOAP_FAULT",
    "missing_reference": "MISSING_REFERENCE",
    "duplicate_cid": "DUPLICATE_CONTENT_ID",
    "digest_mismatch": "SUMMARY_DIGEST_MISMATCH",
}

# complete 场景预期解析出的全部数值（按测量组/通道字面量列出）
EXPECTED_COMPLETE = {
    "instrument_id": "FICT-LAB-DEV-007",
    "groups": [
        {
            "measurement_id": "M-FICT-20261111-0001",
            "sampled_at": "2026-11-11T08:30:00Z",
            "channels": {
                "TEMP_21": {"unit": "Celsius", "values": [21.37, 21.42, 21.48, 21.51]},
                "PRES_04": {"unit": "kPa", "values": [101.32, 101.30, 101.33]},
            },
        },
        {
            "measurement_id": "M-FICT-20261111-0002",
            "sampled_at": "2026-11-11T08:31:00Z",
            "channels": {
                "FLOW_12": {"unit": "mL/min", "values": [42.0, 42.5, 41.9, 43.1, 42.8]},
                "PH_03": {"unit": "pH", "values": [7.01, 7.04, 6.98]},
            },
        },
    ],
}

# parts_reordered 场景（附件先于 XML 到达）的预期数值
EXPECTED_REORDERED = {
    "instrument_id": "FICT-LAB-DEV-017",
    "groups": [
        {
            "measurement_id": "M-FICT-20261111-0301",
            "channels": {
                "TEMP_21": {"values": [18.5, 18.6, 18.7, 18.4]},
                "FLOW_12": {"values": [37.2, 37.0, 37.4]},
            },
        },
    ],
}

# 缺失引用场景：预期被点名的 cid
EXPECTED_MISSING_CID = "M-FICT-20261111-0101.HUM_09@fictional-lab.invalid"

# Fault 场景预期业务信息
EXPECTED_FAULT = {
    "code": "soap:Server",
    "error_kind": "CalibrationExpired",
    "instrument_id": "FICT-LAB-DEV-007",
}

# 重复 CID 场景：冲突应发生在 TEMP_21 这个 cid 上，且两个 part 序号都被记录
EXPECTED_DUPLICATE_CID = "M-FICT-20261111-0201.TEMP_21@fictional-lab.invalid"

# 摘要不匹配场景：被篡改的通道
EXPECTED_MISMATCH_CHANNEL = "PH_03"
EXPECTED_MISMATCH_MEASUREMENT = "M-FICT-20261111-0401"

# LABSMP v1 格式常量（与采集器解析约定独立写出，用于交叉核对）
LABSMP_MAGIC = b"LABSMP01"
LABSMP_ID_FIELD_BYTES = 32

# 演示按钮必须在 15 秒内完成
DEMO_BUDGET_SECONDS = 15.0
