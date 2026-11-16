"""Bounded Git diff collection without target-repository helpers."""

import os
import re
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path

MAX_DIFF_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
GIT_TIMEOUT_SECONDS = 45.0
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GitDiffError(ValueError):
    """A safe, non-reflective Git diff collection error."""


def _git() -> str:
    found = shutil.which("git")
    if not found:
        raise GitDiffError("git is unavailable")
    resolved = Path(found).resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise GitDiffError("git executable is unsafe")
    return str(resolved)


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key in {
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS",
            "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
        } or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key, None)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
    })
    return env


def _resolve(git: str, revision: str, env: dict[str, str], cwd: str | None) -> str:
    try:
        result = subprocess.run(
            [git, "rev-parse", "--verify", f"{revision}^{{commit}}"],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitDiffError("unable to resolve commit identity") from exc
    value = result.stdout.decode("ascii", errors="ignore").strip()
    if result.returncode != 0 or not SHA_RE.fullmatch(value):
        raise GitDiffError("unable to resolve commit identity")
    return value


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_bounded(command: list[str], env: dict[str, str], cwd: str | None, limit: int, timeout: float) -> bytes:
    if type(limit) is not int or limit < 1:
        raise GitDiffError("invalid diff byte limit")
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=0,
            close_fds=True,
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        raise GitDiffError("Git diff collection failed") from exc

    output = bytearray()
    stderr_size = 0
    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate(process)
                raise GitDiffError("Git diff collection timed out")
            events = selector.select(min(0.1, remaining))
            if not events and process.poll() is not None:
                events = [(key, selectors.EVENT_READ) for key in list(selector.get_map().values())]
            for key, _ in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    if len(output) + len(chunk) > limit:
                        _terminate(process)
                        raise GitDiffError("Git diff exceeds byte limit")
                    output.extend(chunk)
                else:
                    stderr_size += len(chunk)
                    if stderr_size > MAX_STDERR_BYTES:
                        _terminate(process)
                        raise GitDiffError("Git diff stderr exceeds byte limit")
        return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if return_code != 0:
            raise GitDiffError("Git diff collection failed")
        return bytes(output)
    except subprocess.TimeoutExpired as exc:
        _terminate(process)
        raise GitDiffError("Git diff collection timed out") from exc
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        _terminate(process)


def collect_git_diff(
    *,
    staged: bool = False,
    base: str = "",
    head: str = "",
    cwd: str | Path | None = None,
    max_bytes: int = MAX_DIFF_BYTES,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> bytes:
    """Collect staged or base-to-head Git diff bytes with strict bounds."""

    if staged and (base or head):
        raise GitDiffError("staged mode cannot use revisions")
    if not staged and bool(base) != bool(head):
        raise GitDiffError("base and head must be supplied together")
    cwd_text = os.fspath(cwd) if cwd is not None else None
    git = _git()
    env = _environment()
    command = [git, "-c", "core.pager=cat", "-c", "diff.external=", "diff"]
    if staged:
        command.append("--cached")
    else:
        if base:
            if not SHA_RE.fullmatch(base) or not SHA_RE.fullmatch(head):
                raise GitDiffError("revisions must be full lowercase commit SHA values")
        else:
            base = _resolve(git, "HEAD^", env, cwd_text)
            head = _resolve(git, "HEAD", env, cwd_text)
        command.append(f"{base}...{head}")
    command.extend(["--no-ext-diff", "--no-textconv", "--binary", "--"])
    return _run_bounded(command, env, cwd_text, max_bytes, timeout)
