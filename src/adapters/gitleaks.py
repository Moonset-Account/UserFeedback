"""Strict redacted Gitleaks JSON/SARIF interoperability."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

from ..scanner import Finding

MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_FINDINGS = 10_000
_TIMEOUT = 60
_SAFE_RULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REDACTED = {"", "REDACTED", "[REDACTED]", "***"}


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError("Gitleaks report contains a duplicate JSON key")
        result[key]=value
    return result


def _safe_path(value):
    if not isinstance(value,str) or not value or "\\" in value or "\x00" in value:raise ValueError("Gitleaks finding path is invalid")
    path=PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:raise ValueError("Gitleaks finding path escapes the repository")
    return path.as_posix()


def _finding(rule,path,line,external_identity):
    if not isinstance(rule,str) or not _SAFE_RULE.fullmatch(rule):raise ValueError("Gitleaks rule ID is invalid")
    if type(line) is not int or line < 1 or line > 10_000_000:raise ValueError("Gitleaks line is invalid")
    identity=external_identity if isinstance(external_identity,str) and len(external_identity)<=512 else f"{path}:{rule}:{line}"
    safe_identity=hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return Finding(type="secret",pattern_name=rule,severity="high",file=path,line=line,
        matched_text=f"external:gitleaks:{rule}:{safe_identity}",line_content="[REDACTED]",rule="Gitleaks imported finding",
        rule_id="CRT-GL-001",category="gitleaks",confidence="high",
        remediation="Rotate the credential if real, remove it from history, and rerun Gitleaks.")


def _json_findings(data):
    if not isinstance(data,list) or len(data)>MAX_FINDINGS:raise ValueError("Gitleaks JSON root must be a bounded array")
    output=[]
    for item in data:
        if not isinstance(item,dict):raise ValueError("Gitleaks JSON finding must be an object")
        for field in ("Secret","Match"):
            value=item.get(field,"")
            if not isinstance(value,str) or value not in _REDACTED:raise ValueError("Gitleaks report is not safely redacted")
        output.append(_finding(item.get("RuleID"),_safe_path(item.get("File")),item.get("StartLine"),item.get("Fingerprint")))
    return output


def _sarif_findings(data):
    if not isinstance(data,dict) or data.get("version")!="2.1.0" or not isinstance(data.get("runs"),list):raise ValueError("Unsupported Gitleaks SARIF structure")
    output=[]
    for run in data["runs"]:
        if not isinstance(run,dict):raise ValueError("Invalid SARIF run")
        results=run.get("results",[])
        if not isinstance(results,list):raise ValueError("Invalid SARIF results")
        for item in results:
            try:
                rule=item["ruleId"]; location=item["locations"][0]["physicalLocation"]
                path=_safe_path(location["artifactLocation"]["uri"]); line=location["region"]["startLine"]
                message=item.get("message",{}).get("text","")
            except (KeyError,IndexError,TypeError) as exc:raise ValueError("Malformed SARIF finding") from exc
            if not isinstance(message,str) or len(message)>2048 or re.search(r'(?:ghp_|AKIA|glpat-|xox[bp]-|sk_live_)',message,re.I):raise ValueError("SARIF message may contain unredacted secret evidence")
            fingerprints=item.get("fingerprints",{})
            if not isinstance(fingerprints,dict):raise ValueError("Malformed SARIF fingerprints")
            output.append(_finding(rule,path,line,fingerprints.get("gitleaksFingerprint")))
            if len(output)>MAX_FINDINGS:raise ValueError("Gitleaks SARIF exceeds finding limit")
    return output


def _safe_read(path: str,limit: int) -> bytes:
    flags=os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_CLOEXEC",0)
    try:descriptor=os.open(path,flags)
    except OSError as exc:raise ValueError("Gitleaks report must be a regular non-symlink file") from exc
    try:
        metadata=os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size>limit:raise ValueError("Gitleaks report exceeds byte limit or is not regular")
        chunks=[]; total=0
        while total<=limit:
            chunk=os.read(descriptor,min(65536,limit+1-total))
            if not chunk:break
            chunks.append(chunk); total+=len(chunk)
        if total>limit:raise ValueError("Gitleaks report exceeds byte limit")
        return b"".join(chunks)
    finally:os.close(descriptor)


def import_gitleaks_report(path: str) -> list[Finding]:
    raw=_safe_read(path,MAX_REPORT_BYTES)
    try:data=json.loads(raw.decode("utf-8"),object_pairs_hook=_unique)
    except (UnicodeDecodeError,json.JSONDecodeError,RecursionError) as exc:raise ValueError("Gitleaks report is not strict bounded UTF-8 JSON") from exc
    findings=_json_findings(data) if isinstance(data,list) else _sarif_findings(data)
    unique={item.fingerprint:item for item in findings}
    return [unique[key] for key in sorted(unique)]


def run_gitleaks(binary_path: str, repo_path: str) -> tuple[list[Finding],str]:
    supplied=Path(binary_path)
    if not supplied.is_absolute():raise ValueError("Gitleaks binary path must be absolute")
    binary=supplied.resolve(strict=True)
    repo=Path(repo_path).resolve(strict=True)
    if not binary.is_file() or not os.access(binary,os.X_OK):raise ValueError("Gitleaks binary must be an executable regular file")
    if not repo.is_dir():raise ValueError("Gitleaks source must be a directory")
    def execute(args):
        try:
            with tempfile.TemporaryFile() as stdout,tempfile.TemporaryFile() as stderr:
                process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,shell=False,start_new_session=True)
                deadline=time.monotonic()+_TIMEOUT
                while process.poll() is None:
                    if os.fstat(stdout.fileno()).st_size>65536 or os.fstat(stderr.fileno()).st_size>65536:
                        process.kill(); process.wait(); raise ValueError("Gitleaks process output exceeds limit")
                    if time.monotonic()>=deadline:
                        process.kill(); process.wait(); raise RuntimeError("Gitleaks execution failed safely: TimeoutExpired")
                    time.sleep(0.01)
                if os.fstat(stdout.fileno()).st_size>65536 or os.fstat(stderr.fileno()).st_size>65536:raise ValueError("Gitleaks process output exceeds limit")
                if process.returncode not in (0,1):raise RuntimeError("Gitleaks process returned an execution error")
                stdout.seek(0); return stdout.read(65537)
        except (OSError,subprocess.SubprocessError) as exc:raise RuntimeError(f"Gitleaks execution failed safely: {type(exc).__name__}") from exc
    version_output=execute([str(binary),"version"])
    version=version_output.decode("utf-8","replace").strip()
    if not version or len(version)>128:raise ValueError("Gitleaks version output is invalid")
    with tempfile.TemporaryDirectory(prefix="crt-gitleaks-") as tmp:
        report=Path(tmp)/"report.json"
        execute([str(binary),"detect","--source",str(repo),"--report-format","json","--report-path",str(report),"--redact","--no-banner"])
        if not report.is_file():raise RuntimeError("Gitleaks did not create the requested report")
        return import_gitleaks_report(str(report)),version
