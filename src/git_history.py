"""Bounded, argument-array-only Git history collection."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

MAX_COMMITS = 1000
MAX_DIFF_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 20
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^/-]{0,255}$")
_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _run(repo: Path, args: list[str], limit: int = MAX_DIFF_BYTES) -> bytes:
    command=["git","-c","core.pager=cat","-c","pager.show=false","-c","diff.external=",*args]
    environment=os.environ.copy(); environment.update({"GIT_PAGER":"cat","GIT_EXTERNAL_DIFF":"","GIT_TERMINAL_PROMPT":"0"})
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process=subprocess.Popen(command,cwd=str(repo),stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,shell=False,env=environment,start_new_session=True)
            deadline=time.monotonic()+COMMAND_TIMEOUT_SECONDS
            while process.poll() is None:
                if os.fstat(stdout.fileno()).st_size>limit or os.fstat(stderr.fileno()).st_size>64*1024:
                    process.kill(); process.wait(); raise ValueError("Git output exceeds the configured byte limit")
                if time.monotonic()>=deadline:
                    process.kill(); process.wait(); raise RuntimeError("Git command failed safely: TimeoutExpired")
                time.sleep(0.01)
            if os.fstat(stdout.fileno()).st_size>limit or os.fstat(stderr.fileno()).st_size>64*1024:
                raise ValueError("Git output exceeds the configured byte limit")
            if process.returncode!=0:raise ValueError("Git command rejected the repository or revision")
            stdout.seek(0); return stdout.read(limit+1)
    except (OSError,subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Git command failed safely: {type(exc).__name__}") from exc


def collect_history_diffs(repo_path: str, since_ref: str | None = None, max_commits: int = 100) -> list[tuple[str, str]]:
    """Return bounded ``(commit_oid, unified_diff)`` entries without shell execution."""
    if type(max_commits) is not int or not 1 <= max_commits <= MAX_COMMITS:
        raise ValueError(f"max_commits must be an integer from 1 to {MAX_COMMITS}")
    if since_ref is not None and (not isinstance(since_ref, str) or not _REF.fullmatch(since_ref) or ".." in since_ref):
        raise ValueError("since_ref is not a safe bounded Git revision")
    repo = Path(repo_path)
    if not repo.is_dir():
        raise ValueError("Git history path must be an existing directory")
    marker = _run(repo, ["rev-parse", "--is-inside-work-tree"], 128).decode("ascii", "strict").strip()
    if marker != "true":
        raise RuntimeError("Path is not a Git work tree")
    revision = f"{since_ref}..HEAD" if since_ref else "HEAD"
    raw_commits = _run(repo, ["rev-list", f"--max-count={max_commits}", revision, "--"], 128 * 1024)
    commits = [line for line in raw_commits.decode("ascii", "strict").splitlines() if line]
    if any(not _OID.fullmatch(commit) for commit in commits):
        raise RuntimeError("Git returned an invalid commit identifier")
    collected: list[tuple[str, str]] = []
    total = 0
    for commit in commits:
        payload = _run(repo, ["show", "--format=", "--no-ext-diff", "--no-textconv", "--no-renames", "--unified=3", commit, "--"])
        total += len(payload)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Git history exceeds the total byte limit")
        collected.append((commit, payload.decode("utf-8", "replace")))
    return collected
