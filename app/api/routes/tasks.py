"""后台任务进度查询接口"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.tasks import get_task_manager

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


@router.get("", summary="后台任务列表(运行中优先)")
def list_tasks(limit: int = 20, running: bool = False) -> dict:
    return {"tasks": get_task_manager().list(limit, include_running_only=running)}


@router.get("/{task_id}", summary="查询后台任务状态与进度")
def get_task(task_id: str) -> dict:
    task = get_task_manager().get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在或已随进程重启丢失")
    return task
