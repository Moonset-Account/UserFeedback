"""实验仪器 SOAP/MTOM 采集器支持的协议子集与错误码定义。"""
from __future__ import annotations

# ---- 支持的协议子集（虚构协议 LabInstr SOAP/MTOM v1） ----
NS_SOAP11 = "http://schemas.xmlsoap.org/soap/envelope/"
NS_SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
NS_XOP = "http://www.w3.org/2004/08/xop/include"
NS_LAB = "urn:fictional-lab-instrument:v1"

# LABSMP v1 二进制测量样本：magic + uint32 数量 + float64 小端序列 + 尾部元数据
LABSMP_MAGIC = b"LABSMP01"
LABSMP_SAMPLE_MEDIA_TYPE = "application/x-fictional-lab-sample"

# 支持的 SOAP 子集说明（写入 README/页面）
SUPPORTED_SUBSET = (
    "LabInstr SOAP/MTOM v1（虚构协议子集）：SOAP 1.1 / 1.2 Envelope；"
    "multipart/related 分块传输（RFC 2387，start 参数或首个 part 定位根）；"
    "xop:Include(href=cid:...) 引用 MIME 附件；Content-ID 关联（与 part 出现顺序无关）；"
    "SOAP Fault；application/x-fictional-lab-sample（LABSMP v1）二进制样本"
)

# ---- 错误码：全部可直接在页面解释 ----
ERR_NOT_MULTIPART = "NOT_MULTIPART"
ERR_ROOT_NOT_FOUND = "ROOT_PART_NOT_FOUND"
ERR_DUPLICATE_CID = "DUPLICATE_CONTENT_ID"
ERR_DTD_FORBIDDEN = "DTD_FORBIDDEN"
ERR_XML_MALFORMED = "XML_MALFORMED"
ERR_SOAP_FAULT = "SOAP_FAULT"
ERR_MISSING_REFERENCE = "MISSING_REFERENCE"
ERR_DANGLING_PART = "DANGLING_ATTACHMENT"
ERR_SAMPLE_FORMAT = "SAMPLE_FORMAT_ERROR"
ERR_MEASUREMENT_ID_MISMATCH = "MEASUREMENT_ID_MISMATCH"
ERR_SUMMARY_MISMATCH = "SUMMARY_DIGEST_MISMATCH"
ERR_HTTP = "HTTP_FETCH_ERROR"
ERR_UNKNOWN_SOURCE = "UNKNOWN_SOURCE"
ERR_INTERNAL = "INTERNAL_ERROR"

ERROR_MESSAGES_CN = {
    ERR_NOT_MULTIPART: "响应不是 multipart/related MIME 消息",
    ERR_ROOT_NOT_FOUND: "无法按 start 参数或首 part 定位 SOAP 根 part",
    ERR_DUPLICATE_CID: "MIME 消息中出现重复 Content-ID，附件归属存在歧义，拒绝按顺序猜测",
    ERR_DTD_FORBIDDEN: "XML 含 DOCTYPE/外部实体声明，已按安全策略拒绝",
    ERR_XML_MALFORMED: "SOAP Envelope XML 格式不合法",
    ERR_SOAP_FAULT: "仪器返回业务 SOAP Fault",
    ERR_MISSING_REFERENCE: "xop:Include 引用的 Content-ID 在 MIME 附件中不存在",
    ERR_DANGLING_PART: "存在未被任何 xop:Include 引用的孤立附件",
    ERR_SAMPLE_FORMAT: "LABSMP v1 样本字节结构不合法",
    ERR_MEASUREMENT_ID_MISMATCH: "附件内嵌测量标识与 XML 字段不一致",
    ERR_SUMMARY_MISMATCH: "摘要（SHA-256 校验字）与通道重算结果不一致",
    ERR_HTTP: "通过规定接口获取响应失败",
    ERR_UNKNOWN_SOURCE: "不支持的输入来源",
    ERR_INTERNAL: "采集器内部错误",
}

# 采集结论状态
STATUS_COMPLETE = "complete"            # 完整且摘要正确，进入正式结果
STATUS_FAULT = "fault"                  # 业务 Fault
STATUS_REJECTED = "rejected"            # 关联/格式/摘要等校验失败
STATUS_FETCH_ERROR = "fetch_error"      # 来源获取失败


class CollectorError(Exception):
    """带错误码与可解释信息的采集器异常。"""

    def __init__(self, code: str, detail: str = "", *, evidence: dict | None = None):
        super().__init__(f"[{code}] {detail or ERROR_MESSAGES_CN.get(code, code)}")
        self.code = code
        self.detail = detail or ERROR_MESSAGES_CN.get(code, code)
        self.evidence = evidence or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": ERROR_MESSAGES_CN.get(self.code, self.code), "detail": self.detail, "evidence": self.evidence}
