"""Strict, bounded stdin contracts for AI-agent post-change hooks."""
import json

MAX_HOOK_BYTES=4*1024*1024
_SCHEMAS={
    "generic":("coderisktools.agent-hook","event",{"post-change"}),
    "codex":("coderisktools.codex-hook","event",{"post-change"}),
    "claude-code":("coderisktools.claude-hook","hook_event_name",{"Stop","PostToolUse"}),
}


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError("Hook payload contains duplicate key")
        result[key]=value
    return result


def parse_hook_payload(agent: str,raw: bytes) -> str:
    if agent not in _SCHEMAS:raise ValueError("Unsupported hook adapter")
    if not isinstance(raw,bytes) or not raw or len(raw)>MAX_HOOK_BYTES:raise ValueError("Hook payload exceeds bounded input contract")
    try:data=json.loads(raw.decode("utf-8"),object_pairs_hook=_unique)
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:raise ValueError("Hook payload is not strict UTF-8 JSON") from exc
    schema,event_key,events=_SCHEMAS[agent]
    expected={"schema","version",event_key,"diff"}
    if not isinstance(data,dict) or set(data)!=expected or data.get("schema")!=schema or type(data.get("version")) is not int or data["version"]!=1 or data.get(event_key) not in events:raise ValueError("Hook payload does not match the selected adapter contract")
    diff=data.get("diff")
    if not isinstance(diff,str) or not diff or len(diff.encode("utf-8"))>MAX_HOOK_BYTES or "\x00" in diff:raise ValueError("Hook diff is invalid or exceeds its limit")
    return diff
