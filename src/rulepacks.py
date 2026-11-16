"""Signed offline rule-pack loading, installation, and rollback."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from .patterns import DetectionRule

MAX_PACK_BYTES=2*1024*1024
MAX_TOTAL_PACK_BYTES=8*1024*1024
MAX_RULES=1000
MAX_TOTAL_RULES=4000
_Q=2**255-19
_L=2**252+27742317777372353535851937790883648493
_D=(-121665*pow(121666,_Q-2,_Q))%_Q
_I=pow(2,(_Q-1)//4,_Q)


def _point_add(p,q):
    x1,y1=p; x2,y2=q; factor=_D*x1*x2*y1*y2%_Q
    return ((x1*y2+x2*y1)*pow(1+factor,_Q-2,_Q)%_Q,(y1*y2+x1*x2)*pow(1-factor,_Q-2,_Q)%_Q)


def _scalar_mult(point,scalar):
    result=(0,1); current=point
    while scalar:
        if scalar&1:result=_point_add(result,current)
        current=_point_add(current,current); scalar>>=1
    return result


def _recover_x(y,sign):
    x=pow((y*y-1)*pow(_D*y*y+1,_Q-2,_Q)%_Q,(_Q+3)//8,_Q)
    if (x*x-((y*y-1)*pow(_D*y*y+1,_Q-2,_Q)))%_Q:x=x*_I%_Q
    if x&1 != sign:x=_Q-x
    return x


def _decode_point(encoded):
    if len(encoded)!=32:raise ValueError("Invalid Ed25519 point length")
    value=int.from_bytes(encoded,"little"); y=value&((1<<255)-1); sign=value>>255
    if y>=_Q:raise ValueError("Invalid Ed25519 point")
    x=_recover_x(y,sign)
    if (-x*x+y*y-1-_D*x*x*y*y)%_Q:raise ValueError("Ed25519 point is not on curve")
    return x,y


def _prime_subgroup(point):
    return point!=(0,1) and _scalar_mult(point,_L)==(0,1)


def verify_ed25519(public_key: bytes,message: bytes,signature: bytes) -> bool:
    try:
        if len(public_key)!=32 or len(signature)!=64:return False
        r_encoded=signature[:32]; scalar=int.from_bytes(signature[32:],"little")
        if scalar>=_L:return False
        public=_decode_point(public_key); r_point=_decode_point(r_encoded)
        if not _prime_subgroup(public) or not _prime_subgroup(r_point):return False
        base_y=4*pow(5,_Q-2,_Q)%_Q; base=(_recover_x(base_y,0),base_y)
        challenge=int.from_bytes(hashlib.sha512(r_encoded+public_key+message).digest(),"little")%_L
        return _scalar_mult(base,scalar)==_point_add(r_point,_scalar_mult(public,challenge))
    except (ValueError,ArithmeticError):return False


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError("Rule pack contains a duplicate JSON key")
        result[key]=value
    return result


def _read(path):
    flags=os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_CLOEXEC",0)
    try:descriptor=os.open(path,flags)
    except OSError as exc:raise ValueError("Rule pack must be a regular non-symlink file") from exc
    try:
        metadata=os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size>MAX_PACK_BYTES:raise ValueError("Rule pack exceeds byte limit or is not regular")
        chunks=[]; total=0
        while total<=MAX_PACK_BYTES:
            chunk=os.read(descriptor,min(65536,MAX_PACK_BYTES+1-total))
            if not chunk:break
            chunks.append(chunk); total+=len(chunk)
        if total>MAX_PACK_BYTES:raise ValueError("Rule pack exceeds byte limit")
        raw=b"".join(chunks)
    finally:os.close(descriptor)
    try:return raw,json.loads(raw.decode("utf-8"),object_pairs_hook=_unique)
    except (UnicodeDecodeError,json.JSONDecodeError,RecursionError) as exc:raise ValueError("Rule pack is not strict bounded UTF-8 JSON") from exc


def _validate_regex(value):
    if not isinstance(value,str) or not 1<=len(value)<=512:raise ValueError("Rule regex has invalid length")
    outside=re.sub(r"\\.|\[[^\]]*\]","",value)
    if any(character in outside for character in "().|*+?"):raise ValueError("Rule regex exceeds the safe linear grammar")
    quantifiers=re.findall(r"\{([^{}]*)\}",outside)
    if outside.count("{")!=len(quantifiers) or outside.count("}")!=len(quantifiers):raise ValueError("Rule regex has malformed quantifier")
    for quantifier in quantifiers:
        if not quantifier.isdigit() or not 1<=int(quantifier)<=256:raise ValueError("Rule regex permits only bounded exact quantifiers")
    try:re.compile(value)
    except re.error as exc:raise ValueError("Rule regex does not compile") from exc
    return value


def load_trusted_keyring(path: str) -> dict[str,bytes]:
    _raw,data=_read(path)
    if not isinstance(data,dict) or set(data)!={"schema","version","keys"}:raise ValueError("Rule keyring has an invalid schema")
    if data["schema"]!="coderisktools.rule-keyring" or type(data["version"]) is not int or data["version"]!=1:raise ValueError("Unsupported rule keyring")
    keys=data["keys"]
    if not isinstance(keys,dict) or not 1<=len(keys)<=64:raise ValueError("Rule keyring keys must be a bounded object")
    result={}
    for key_id,value in keys.items():
        if not isinstance(key_id,str) or not re.fullmatch(r"[a-z][a-z0-9._-]{2,63}",key_id) or not isinstance(value,str) or not re.fullmatch(r"[0-9a-f]{64}",value):raise ValueError("Rule keyring entry is invalid")
        result[key_id]=bytes.fromhex(value)
    return result


def _load_rule_pack(path: str,trusted_keys: dict[str,bytes]) -> tuple[list[DetectionRule],bytes]:
    raw,data=_read(path)
    if not isinstance(data,dict) or set(data)!={"schema","version","key_id","pack","signature"}:raise ValueError("Rule pack envelope has an invalid schema")
    if data["schema"]!="coderisktools.rule-pack" or type(data["version"]) is not int or data["version"]!=1:raise ValueError("Unsupported rule pack schema or version")
    key_id=data["key_id"]
    if not isinstance(key_id,str) or key_id not in trusted_keys:raise ValueError("Rule pack signing key is not trusted")
    key=trusted_keys[key_id]
    if not isinstance(key,bytes) or len(key)!=32:raise ValueError("Trusted Ed25519 public key is invalid")
    signature=data["signature"]
    if not isinstance(signature,str) or not re.fullmatch(r"[0-9a-f]{128}",signature):raise ValueError("Rule pack signature is missing or invalid")
    signed={key:data[key] for key in ("schema","version","key_id","pack")}
    message=json.dumps(signed,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")
    if not verify_ed25519(key,message,bytes.fromhex(signature)):raise ValueError("Rule pack signature verification failed")
    pack=data["pack"]
    if not isinstance(pack,dict) or set(pack)!={"pack_version","rules"} or not isinstance(pack["pack_version"],str) or not re.fullmatch(r"[0-9]{4}\.[0-9]{2}\.[0-9]+",pack["pack_version"]):raise ValueError("Rule pack metadata is invalid")
    entries=pack["rules"]
    if not isinstance(entries,list) or not 1<=len(entries)<=MAX_RULES:raise ValueError("Rule pack rules must be a bounded non-empty array")
    rules=[]; seen=set()
    required={"name","regex","severity","description","rule_id","category","confidence","remediation","kind","file_globs"}
    for item in entries:
        if not isinstance(item,dict) or set(item)!=required:raise ValueError("Rule entry has an invalid schema")
        if not isinstance(item["name"],str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}",item["name"]):raise ValueError("Rule name is invalid")
        if not isinstance(item["rule_id"],str) or not re.fullmatch(r"CRT-(?:SEC|POL)-[0-9]{3}",item["rule_id"]) or item["rule_id"] in seen:raise ValueError("Rule ID is invalid or duplicated")
        if item["severity"] not in {"low","medium","high","critical"} or item["confidence"] not in {"low","medium","high"} or item["kind"] not in {"secret","policy"}:raise ValueError("Rule enum value is invalid")
        if not isinstance(item["category"],str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}",item["category"]):raise ValueError("Rule category is invalid")
        if not all(isinstance(item[field],str) and 1<=len(item[field])<=512 for field in ("description","remediation")):raise ValueError("Rule text is invalid")
        globs=item["file_globs"]
        if not isinstance(globs,list) or len(globs)>32 or not all(isinstance(g,str) and 1<=len(g)<=128 and ".." not in g and "\\" not in g for g in globs):raise ValueError("Rule file globs are invalid")
        seen.add(item["rule_id"])
        rules.append(DetectionRule(name=item["name"],regex=_validate_regex(item["regex"]),severity=item["severity"],description=item["description"],rule_id=item["rule_id"],category=item["category"],confidence=item["confidence"],remediation=item["remediation"],kind=item["kind"],file_globs=tuple(globs)))
    return rules,raw


def load_rule_pack(path: str,trusted_keys: dict[str,bytes]) -> list[DetectionRule]:
    return _load_rule_pack(path,trusted_keys)[0]


def load_rule_packs(paths: list[str], trusted_keys: dict[str,bytes]) -> list[DetectionRule]:
    """Load multiple signed packs as one deterministic v2 registry input.

    Each member remains independently signature-verified and schema-validated.
    The aggregate rejects duplicate IDs and enforces global byte/rule bounds.
    """
    if not isinstance(paths, list) or not 1 <= len(paths) <= 64:
        raise ValueError("Rule pack paths must be a bounded non-empty list")
    rules: list[DetectionRule] = []
    seen: set[str] = set()
    total_bytes = 0
    for path in paths:
        loaded, raw = _load_rule_pack(path, trusted_keys)
        total_bytes += len(raw)
        if total_bytes > MAX_TOTAL_PACK_BYTES or len(rules) + len(loaded) > MAX_TOTAL_RULES:
            raise ValueError("Combined rule packs exceed aggregate bounds")
        for rule in loaded:
            if rule.rule_id in seen:
                raise ValueError(f"Combined rule packs contain duplicate rule ID: {rule.rule_id}")
            seen.add(rule.rule_id)
            rules.append(rule)
    return rules


def _atomic_write(destination: Path,payload: bytes):
    if not destination.parent.is_dir() or destination.is_symlink():raise ValueError("Rule pack destination is unsafe")
    temporary=None
    try:
        fd,name=tempfile.mkstemp(prefix=f".{destination.name}.",suffix=".tmp",dir=str(destination.parent)); temporary=Path(name)
        with os.fdopen(fd,"wb") as stream:
            os.chmod(name,0o600); stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,destination); temporary=None
        directory_fd=os.open(destination.parent,os.O_RDONLY)
        try:os.fsync(directory_fd)
        finally:os.close(directory_fd)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def install_rule_pack(source_path: str,destination_path: str,trusted_keys: dict[str,bytes]):
    _rules,source_bytes=_load_rule_pack(source_path,trusted_keys)
    destination=Path(destination_path); rollback=Path(str(destination)+".rollback")
    if destination.exists():
        _existing_rules,existing_bytes=_load_rule_pack(str(destination),trusted_keys)
        _atomic_write(rollback,existing_bytes)
    _atomic_write(destination,source_bytes)


def rollback_rule_pack(destination_path: str,trusted_keys: dict[str,bytes]):
    destination=Path(destination_path); rollback=Path(str(destination)+".rollback")
    _rules,rollback_bytes=_load_rule_pack(str(rollback),trusted_keys)
    _atomic_write(destination,rollback_bytes)
