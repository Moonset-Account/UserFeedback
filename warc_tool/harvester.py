"""采集器：仅通过公开 HTTP 接口抓取本地源站并写 WARC。

与样例生成器（samplegen）分离：
* 采集器不导入 source_content，不知道页面真值，只用 urllib 发 HTTP 请求；
* 每个页面写一对 request / response 记录，request 通过
  WARC-Concurrent-To 关联对应 response；
* 同 URL 多版本通过源站公开的 X-Sim-Date 请求头分别采集，
  WARC-Date 取采集时刻参数（由调用方给出确定性时间戳）。
"""
from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass

from .warc_sink import WarcSink


@dataclass
class CaptureSpec:
    url: str
    warc_date: str
    sim_date: str | None = None
    method: str = "GET"


def _do_request(spec: CaptureSpec, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(spec.url, method=spec.method)
    req.add_header("User-Agent", "xinglan-warc-harvester/1.0 (local-demo)")
    if spec.sim_date:
        req.add_header("X-Sim-Date", spec.sim_date)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
            reason = getattr(resp, "reason", "OK")
            headers = list(resp.headers.items())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
        reason = exc.reason
        headers = list(exc.headers.items()) if exc.headers else []
    request_headers = list(req.header_items())
    return {
        "status": status,
        "reason": reason,
        "headers": headers,
        "body": body,
        "request_headers": request_headers,
    }


def harvest(specs: list[CaptureSpec], output_path: str, gzip: bool = True) -> dict:
    """按规格列表采集并写 WARC，返回简要统计。"""
    written = []
    with WarcSink(output_path, gzip=gzip) as sink:
        sink.write_warcinfo({
            "software": "xinglan-warc-harvester/1.0",
            "format": "WARC File Format 1.1",
            "note": "全部内容来自本地虚构源站的公开 HTTP 接口",
        })
        for spec in specs:
            result = _do_request(spec)
            resp_id = sink.write_response(
                uri=spec.url,
                date=spec.warc_date,
                status=result["status"],
                reason=result["reason"],
                http_headers=result["headers"],
                body=result["body"],
            )
            req_id = sink.write_request(
                uri=spec.url,
                date=spec.warc_date,
                method=spec.method,
                http_headers=result["request_headers"],
                concurrent_to=f"<{resp_id}>",
            )
            written.append({
                "url": spec.url,
                "warc_date": spec.warc_date,
                "sim_date": spec.sim_date,
                "response_record_id": resp_id,
                "request_record_id": req_id,
                "http_status": result["status"],
                "bytes": len(result["body"]),
            })
    return {"output": output_path, "captures": written, "count": len(written)}
