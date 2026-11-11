"""公开接口采集客户端。

采集边界（强约束）
==================
* 仅通过本地源站的公开 HTTP/1.1 接口采集任务规格（requests 2.x，GET）；
* 不读取任何本地任务或真值目录文件，也不允许访问仅供测试的独立真值；
* 运行期的页面数值仅通过浏览器 DOM 公开元素读取（见 engine.py）。
"""
from __future__ import annotations

from typing import Any

import requests

from .task_schema import Task, parse_task


class CollectorError(RuntimeError):
    """采集阶段失败（网络、HTTP 状态、协议校验等）。"""


class HttpTaskCollector:
    """经源站公开 REST 接口采集任务的客户端。"""

    def __init__(self, base_url: str, timeout: float = 8.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get_json(self, path: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise CollectorError(f"请求公开接口失败 {url}: {exc}") from exc
        if resp.status_code != 200:
            raise CollectorError(f"公开接口返回非 200 状态: {url} -> {resp.status_code}")
        ctype = resp.headers.get("Content-Type", "")
        if "application/json" not in ctype:
            raise CollectorError(f"响应不是 application/json: {ctype}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise CollectorError(f"响应 JSON 解析失败 {url}: {exc}") from exc
        if not isinstance(payload, dict):
            raise CollectorError("响应根节点必须是 JSON 对象")
        return payload

    def list_tasks(self) -> list[dict[str, str]]:
        payload = self._get_json("/api/tasks")
        tasks = payload.get("tasks", [])
        if not isinstance(tasks, list):
            raise CollectorError("/api/tasks 的 tasks 字段必须是数组")
        return tasks

    def fetch_task(self, task_id: str) -> Task:
        if not isinstance(task_id, str) or not task_id.replace("-", "").isalnum():
            raise CollectorError("非法 task_id")
        payload = self._get_json(f"/api/tasks/{task_id}")
        return parse_task(payload)
