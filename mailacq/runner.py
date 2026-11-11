"""任务编排：为每个 UID 代际启动模拟 IMAP 服务，采集器经协议拉取并解析。

数据流（严格单向，采集器不接触真值）：
  生成器文件 -> MailboxState -> MockIMAPServer --IMAP 协议--> imap_client
       -> parser -> extractor -> identity -> RunRecord -> instance/runs/
"""
from __future__ import annotations

import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field

from .config import MOCK_PASSWORD, MOCK_USERNAME, SUPPORTED_IMAP_SUBSET
from .generator.generate import load_generation_messages
from .collector.extractor import extract
from .collector.identity import analyze, make_identity_key
from .collector.imap_client import collect
from .collector.mock_imap import MailboxState, start_server
from .collector.parser import parse_message


@dataclass
class MessageResult:
    identity_key: str
    uidvalidity: int
    uid: int
    message_id: str | None
    raw_subject: str | None
    decoded_subject: str | None
    parsed: dict
    extraction: dict
    errors: list[str]


@dataclass
class GenerationResult:
    index: int
    uidvalidity: int
    server_host: str
    server_port: int
    command_log: list[str]
    protocol_log: list[str]
    messages: list[MessageResult]


@dataclass
class RunRecord:
    run_id: str
    scenario: str
    started_at: float
    finished_at: float
    duration_ms: int
    status: str  # success / partial / error
    protocol_subset: str
    generations: list[GenerationResult]
    identity: dict
    envelope_summary: list[dict]
    errors: list[str]
    warnings: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def run_scenario(scenario_key: str, *, host: str = "127.0.0.1") -> RunRecord:
    started = time.time()
    run_id = uuid.uuid4().hex[:12]
    errors: list[str] = []
    warnings: list[str] = []
    gen_results: list[GenerationResult] = []
    envelopes: list[dict] = []

    # 先确认场景有几代（通过生成器清单，而非真值）
    from .generator.generate import load_manifest
    manifest = load_manifest(scenario_key)
    generation_count = len(manifest["generations"])

    for gi in range(generation_count):
        uidvalidity, message_bytes = load_generation_messages(scenario_key, gi)
        state = MailboxState(uidvalidity=uidvalidity, messages=message_bytes)
        server = start_server(state, host=host, port=0)
        srv_host, srv_port = server.address
        msg_results: list[MessageResult] = []
        try:
            result = collect(srv_host, srv_port, MOCK_USERNAME, MOCK_PASSWORD)
            if result.uidvalidity != uidvalidity:
                errors.append(
                    f"代际 {gi}: 采集到的 UIDVALIDITY {result.uidvalidity} "
                    f"与服务状态 {uidvalidity} 不一致")
            for fetched in result.messages:
                part_errors: list[str] = []
                try:
                    parsed = parse_message(fetched.raw)
                    extraction = extract(parsed).to_dict()
                    for att in extraction["attachments"]:
                        if att.get("decode_error"):
                            part_errors.append(
                                f"附件 part {att['part_id']} 解码错误: {att['decode_error']}")
                    if extraction["batch"].get("conflict"):
                        warnings.append(
                            f"{make_identity_key(fetched.uidvalidity, fetched.uid)}: "
                            f"{extraction['batch']['conflict_detail']}")
                    key = make_identity_key(fetched.uidvalidity, fetched.uid)
                    msg_results.append(MessageResult(
                        identity_key=key,
                        uidvalidity=fetched.uidvalidity,
                        uid=fetched.uid,
                        message_id=parsed.message_id,
                        raw_subject=parsed.raw_subject,
                        decoded_subject=parsed.decoded_subject,
                        parsed=parsed.to_dict(),
                        extraction=extraction,
                        errors=part_errors,
                    ))
                    envelopes.append({
                        "uidvalidity": fetched.uidvalidity,
                        "uid": fetched.uid,
                        "message_id": parsed.message_id,
                        "decoded_subject": parsed.decoded_subject,
                    })
                    errors.extend(part_errors)
                except Exception as exc:  # 单封邮件失败不应中断整个任务
                    tb = traceback.format_exc(limit=3)
                    errors.append(f"代际 {gi} UID {fetched.uid} 解析失败: {exc}\n{tb}")
            gen_results.append(GenerationResult(
                index=gi, uidvalidity=uidvalidity,
                server_host=srv_host, server_port=srv_port,
                command_log=server.command_log(),
                protocol_log=result.protocol_log,
                messages=msg_results,
            ))
        finally:
            server.shutdown()
            server.server_close()

    identity = analyze(envelopes).to_dict()
    finished = time.time()
    hard_errors = [e for e in errors if "解析失败" in e or "不一致" in e]
    status = "error" if hard_errors and not any(g.messages for g in gen_results) else (
        "partial" if errors else "success")

    return RunRecord(
        run_id=run_id,
        scenario=scenario_key,
        started_at=started,
        finished_at=finished,
        duration_ms=round((finished - started) * 1000),
        status=status,
        protocol_subset=SUPPORTED_IMAP_SUBSET,
        generations=gen_results,
        identity=identity,
        envelope_summary=envelopes,
        errors=errors,
        warnings=warnings,
    )
