"""虚构实验仪器的确定性测量数据集（全部为虚构数据，无真实账户/业务）。

六个场景：
  complete          完整结果：两个测量组、各自两个通道，摘要全部正确
  fault             业务 SOAP Fault（仪器校准过期）
  missing_reference xop:Include 引用的 Content-ID 在附件中不存在
  duplicate_cid     两个附件 part 使用同一 Content-ID（归属歧义）
  parts_reordered   附件先于 XML 根 part 到达，验证不依赖出现顺序
  digest_mismatch   XML 摘要被故意改错，测量组被拒绝
"""
from __future__ import annotations

# 通道编码 → 采样点（确定性虚构数值）
DATASETS = {
    "complete": {
        "instrument_id": "FICT-LAB-DEV-007",
        "measurements": [
            {
                "measurement_id": "M-FICT-20261111-0001",
                "sampled_at": "2026-11-11T08:30:00Z",
                "channels": [
                    {"code": "TEMP_21", "unit": "Celsius", "values": [21.37, 21.42, 21.48, 21.51]},
                    {"code": "PRES_04", "unit": "kPa", "values": [101.32, 101.30, 101.33]},
                ],
            },
            {
                "measurement_id": "M-FICT-20261111-0002",
                "sampled_at": "2026-11-11T08:31:00Z",
                "channels": [
                    {"code": "FLOW_12", "unit": "mL/min", "values": [42.0, 42.5, 41.9, 43.1, 42.8]},
                    {"code": "PH_03", "unit": "pH", "values": [7.01, 7.04, 6.98]},
                ],
            },
        ],
    },
    "fault": {
        "fault": {
            "code": "soap:Server",
            "reason": "仪器 FICT-LAB-DEV-007 校准证书已过期，拒绝执行测量查询",
            "detail": {
                "InstrumentId": "FICT-LAB-DEV-007",
                "CalibrationExpiry": "2026-10-31T00:00:00Z",
                "ErrorKind": "CalibrationExpired",
            },
        }
    },
    "missing_reference": {
        "instrument_id": "FICT-LAB-DEV-011",
        "measurements": [
            {
                "measurement_id": "M-FICT-20261111-0101",
                "sampled_at": "2026-11-11T09:00:00Z",
                "channels": [
                    {"code": "TEMP_21", "unit": "Celsius", "values": [19.9, 20.0, 20.1]},
                    {"code": "HUM_09", "unit": "%RH", "values": [55.0, 55.2]},  # 附件将被故意丢弃
                ],
            },
        ],
    },
    "duplicate_cid": {
        "instrument_id": "FICT-LAB-DEV-013",
        "measurements": [
            {
                "measurement_id": "M-FICT-20261111-0201",
                "sampled_at": "2026-11-11T09:10:00Z",
                "channels": [
                    {"code": "TEMP_21", "unit": "Celsius", "values": [23.1, 23.2]},
                    {"code": "PRES_04", "unit": "kPa", "values": [100.9, 101.0]},
                ],
            },
        ],
    },
    "parts_reordered": {
        "instrument_id": "FICT-LAB-DEV-017",
        "measurements": [
            {
                "measurement_id": "M-FICT-20261111-0301",
                "sampled_at": "2026-11-11T09:20:00Z",
                "channels": [
                    {"code": "TEMP_21", "unit": "Celsius", "values": [18.5, 18.6, 18.7, 18.4]},
                    {"code": "FLOW_12", "unit": "mL/min", "values": [37.2, 37.0, 37.4]},
                ],
            },
        ],
    },
    "digest_mismatch": {
        "instrument_id": "FICT-LAB-DEV-019",
        "measurements": [
            {
                "measurement_id": "M-FICT-20261111-0401",
                "sampled_at": "2026-11-11T09:30:00Z",
                "channels": [
                    {"code": "TEMP_21", "unit": "Celsius", "values": [25.0, 25.1, 25.2]},
                    {"code": "PH_03", "unit": "pH", "values": [6.8, 6.9]},
                ],
            },
        ],
    },
}

# 场景顺序（页面与验收使用）
SCENARIO_ORDER = [
    "complete",
    "fault",
    "missing_reference",
    "duplicate_cid",
    "parts_reordered",
    "digest_mismatch",
]

# 快速演示只跑：一个正常 + 一个边界（重复标识冲突）
DEMO_SCENARIOS = ["complete", "duplicate_cid"]

SCENARIO_TITLES_CN = {
    "complete": "完整结果（摘要全部正确）",
    "fault": "业务 SOAP Fault",
    "missing_reference": "缺失引用（Content-ID 无附件）",
    "duplicate_cid": "重复 Content-ID 冲突",
    "parts_reordered": "附件先于 XML 到达（乱序关联）",
    "digest_mismatch": "摘要不匹配（测量组被拒绝）",
}
