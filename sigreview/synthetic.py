"""本地合成记录构造器与固定场景目录。

所有数据均在本地确定性生成，不联网、不读取真实凭据。场景目录
:data:`INTERFACE_SPECS` 刻意覆盖题面要求的各类边界与交错情况：

* 接口 A：正例 + 用途不符负例，严格模式下成对，限制生效（记功）；
* 接口 B：正例 + 用途不符负例，但严格拒绝后兼容策略继续放行（缺口）；
* 接口 C：正例 + 签名错误负例（负例提前拒绝，不能给用途限制记功）；
* 接口 D：只有用途不符负例，缺正例；
* 接口 E：只有正例，缺负例；
* 接口 F：两组摘要，一组成对记功、另一组缺正例（同接口内交错）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .verifier import (
    Certificate,
    CompatibilityPolicy,
    Purpose,
    SignedRequest,
    Trace,
    VerificationEngine,
    compute_digest,
    sign_payload,
)

# 合成 HMAC 密钥；仅用于驱动本地判定逻辑，不是真实凭据。
SYNTHETIC_SECRET = b"sigreview-local-synthetic-secret"


@dataclass(frozen=True)
class InterfaceSpec:
    """一个合成接口的期望用途与默认引擎模式。"""

    interface: str
    expected_purpose: Purpose
    fallback_to_compatibility: bool = False


# 固定接口目录：接口名 -> 规格
INTERFACE_SPECS: Dict[str, InterfaceSpec] = {
    "payments.submit": InterfaceSpec("payments.submit", Purpose.TLS_CLIENT),
    "firmware.upload": InterfaceSpec(
        "firmware.upload", Purpose.CODE_SIGNING, fallback_to_compatibility=True
    ),
    "mail.relay": InterfaceSpec("mail.relay", Purpose.EMAIL_PROTECTION),
    "reports.export": InterfaceSpec("reports.export", Purpose.DOCUMENT_SIGNING),
    "clock.anchor": InterfaceSpec("clock.anchor", Purpose.TIME_STAMPING),
    "devices.enroll": InterfaceSpec("devices.enroll", Purpose.TLS_SERVER),
}


class RecordBuilder:
    """逐步构造合成请求并交给引擎产出轨迹。

    典型用法::

        builder = RecordBuilder()
        good = builder.signed_request("payments.submit", b"payload-1",
                                      purposes={Purpose.TLS_CLIENT})
        bad_purpose = builder.signed_request("payments.submit", b"payload-1",
                                             purposes={Purpose.TLS_SERVER})
    """

    def __init__(self, secret: bytes = SYNTHETIC_SECRET) -> None:
        self.secret = secret
        self._counter = 0

    def _next_id(self, interface: str) -> str:
        self._counter += 1
        return f"req-{interface}-{self._counter:03d}"

    def make_certificate(
        self, serial: str, subject: str, purposes: Sequence[Purpose]
    ) -> Certificate:
        return Certificate(
            serial=serial,
            subject=subject,
            purposes=frozenset(purposes),
        )

    def signed_request(
        self,
        interface: str,
        payload: bytes,
        purposes: Sequence[Purpose],
        *,
        expected_purpose: Optional[Purpose] = None,
        serial: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> SignedRequest:
        """构造一条签名正确的请求；正反例仅证书用途不同。"""
        spec = INTERFACE_SPECS.get(interface)
        if expected_purpose is None:
            if spec is None:
                raise KeyError(f"未知接口 {interface!r}，需显式给出 expected_purpose")
            expected_purpose = spec.expected_purpose
        self._counter += 1
        request_id = f"req-{interface}-{self._counter:03d}"
        cert = self.make_certificate(
            serial=serial or f"cert-{self._counter:03d}",
            subject=subject or f"CN=synthetic-{self._counter:03d}",
            purposes=purposes,
        )
        return SignedRequest(
            request_id=request_id,
            interface=interface,
            data_digest=compute_digest(payload),
            signature=sign_payload(payload, self.secret),
            certificate=cert,
            expected_purpose=expected_purpose,
            payload=payload,
        )

    def tamper_signature(self, request: SignedRequest) -> SignedRequest:
        """在其它条件不变的情况下破坏签名值。"""
        raw = bytes.fromhex(request.signature)
        tampered = bytes([raw[0] ^ 0x01]) + raw[1:]
        return SignedRequest(
            request_id=request.request_id + "-tamp",
            interface=request.interface,
            data_digest=request.data_digest,
            signature=tampered.hex(),
            certificate=request.certificate,
            expected_purpose=request.expected_purpose,
            payload=request.payload,
        )

    def engine_for(self, interface: str) -> VerificationEngine:
        spec = INTERFACE_SPECS[interface]
        return VerificationEngine(
            secret=self.secret,
            fallback_to_compatibility=spec.fallback_to_compatibility,
            compatibility_policy=CompatibilityPolicy(),
        )

    def run(self, requests: Sequence[SignedRequest]) -> List[Trace]:
        """按请求各自接口的引擎模式验签，返回轨迹列表。"""
        engines: Dict[str, VerificationEngine] = {}
        traces: List[Trace] = []
        for request in requests:
            engine = engines.setdefault(
                request.interface, self.engine_for(request.interface)
            )
            traces.append(engine.verify(request))
        return traces


# ---------------------------------------------------------------------------
# 固定场景目录
# ---------------------------------------------------------------------------


def _pair_payloads() -> Dict[str, bytes]:
    """每个接口的正反例共用同一份 payload，保证数据摘要一致。"""
    return {
        "payments.submit": b"payment-order-A-0001",
        "firmware.upload": b"firmware-image-B-0002",
        "mail.relay": b"mail-message-C-0003",
        "reports.export": b"report-bundle-D-0004",
        "clock.anchor": b"time-stamp-E-0005",
        "devices.enroll": b"device-enroll-F-0006",
    }


def build_fixture_traces() -> List[Trace]:
    """生成覆盖正常 / 边界 / 交错场景的固定轨迹集合。"""
    builder = RecordBuilder()
    payloads = _pair_payloads()
    requests: List[SignedRequest] = []

    # 接口 A：严格模式，正例 + 用途不符负例 -> 成对记功
    a_good = builder.signed_request(
        "payments.submit", payloads["payments.submit"], [Purpose.TLS_CLIENT]
    )
    a_bad_purpose = builder.signed_request(
        "payments.submit",
        payloads["payments.submit"],
        [Purpose.TLS_SERVER],
        serial="cert-A-neg",
    )
    requests.extend([a_good, a_bad_purpose])

    # 接口 B：回退兼容，正例 + 用途不符负例 -> 严格拒绝后兼容放行（缺口）
    b_good = builder.signed_request(
        "firmware.upload", payloads["firmware.upload"], [Purpose.CODE_SIGNING]
    )
    b_bad_purpose = builder.signed_request(
        "firmware.upload",
        payloads["firmware.upload"],
        [Purpose.DOCUMENT_SIGNING],
        serial="cert-B-neg",
    )
    requests.extend([b_good, b_bad_purpose])

    # 接口 C：正例 + 签名错误负例 -> 负例提前拒绝，不给用途限制记功
    c_good = builder.signed_request(
        "mail.relay", payloads["mail.relay"], [Purpose.EMAIL_PROTECTION]
    )
    c_bad_sig = builder.tamper_signature(
        builder.signed_request(
            "mail.relay",
            payloads["mail.relay"],
            [Purpose.TLS_CLIENT],
            serial="cert-C-neg",
        )
    )
    requests.extend([c_good, c_bad_sig])

    # 接口 D：只有用途不符负例，缺正例
    d_bad_purpose = builder.signed_request(
        "reports.export",
        payloads["reports.export"],
        [Purpose.EMAIL_PROTECTION],
        serial="cert-D-neg",
    )
    requests.append(d_bad_purpose)

    # 接口 E：只有正例，缺负例
    e_good = builder.signed_request(
        "clock.anchor", payloads["clock.anchor"], [Purpose.TIME_STAMPING]
    )
    requests.append(e_good)

    # 接口 F：两组摘要交错——第一组成对记功，第二组只有负例（缺正例）
    f_payload_1 = b"device-enroll-F1"
    f_good_1 = builder.signed_request(
        "devices.enroll", f_payload_1, [Purpose.TLS_SERVER]
    )
    f_bad_1 = builder.signed_request(
        "devices.enroll",
        f_payload_1,
        [Purpose.TLS_CLIENT],
        serial="cert-F1-neg",
    )
    f_payload_2 = b"device-enroll-F2"
    f_bad_2 = builder.signed_request(
        "devices.enroll",
        f_payload_2,
        [Purpose.CODE_SIGNING],
        serial="cert-F2-neg",
    )
    requests.extend([f_good_1, f_bad_1, f_bad_2])

    return builder.run(requests)
