"""轻量后台任务管理器: 文档向量化等耗时操作异步执行, 不阻塞 API 服务。

任务在守护线程中运行; 通过 task_id 查询进度(current/done/total)。
进程内存态存储, 重启后任务记录丢失(已写入的向量数据不受影响)。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

_LOCK = threading.Lock()


class TaskManager:
    def __init__(self) -> None:
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def submit(self, task_type: str, fn: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        task_id = uuid.uuid4().hex[:12]
        task: Dict[str, Any] = {
            "id": task_id,
            "type": task_type,
            "status": "running",
            "progress": {"done": 0, "total": None, "current": ""},
            "result": None,
            "errors": [],
            "created_at": time.time(),
            "finished_at": None,
        }
        with _LOCK:
            self._tasks[task_id] = task

        def _run() -> None:
            try:
                task["result"] = fn(task) or None
                task["status"] = "done"
            except Exception as exc:  # noqa: BLE001
                task["status"] = "failed"
                task["errors"].append(str(exc))
            finally:
                task["finished_at"] = time.time()

        threading.Thread(target=_run, daemon=True, name=f"task-{task_id}").start()
        return task

    def update(
        self,
        task_id: str,
        done: Optional[int] = None,
        total: Optional[int] = None,
        current: Optional[str] = None,
    ) -> None:
        with _LOCK:
            task = self._tasks.get(task_id)
            if not task:
                return
            if done is not None:
                task["progress"]["done"] = done
            if total is not None:
                task["progress"]["total"] = total
            if current is not None:
                task["progress"]["current"] = current

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with _LOCK:
            return self._tasks.get(task_id)

    def list(self, limit: int = 20, include_running_only: bool = False) -> list:
        with _LOCK:
            items = list(self._tasks.values())[-limit:]
        if include_running_only:
            items = [t for t in items if t["status"] == "running"]
        # 运行中的排前面, 其余按创建时间倒序
        items.sort(key=lambda t: (-(t["status"] == "running"), -t["created_at"]))
        return items


_manager: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    global _manager
    if _manager is None:
        _manager = TaskManager()
    return _manager
