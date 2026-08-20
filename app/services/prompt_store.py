"""提示词热更新: 在线编辑 System Prompt, 保存即生效(无需重启)。

实现: 生成阶段在每次调用时动态读取当前 System Prompt(而非模块加载时固定),
缺省(未热更 / 被重置)时回退到内置默认 GENERATE_SYSTEM。
持久化到 data/prompts.json, 重启后仍保持。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

from app.config import get_settings


class PromptStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict = {"generate_system": "", "updated_at": 0, "updated_by": ""}
        self._lock = threading.Lock()
        if path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists():
                try:
                    self._data.update(json.loads(p.read_text(encoding="utf-8")))
                except Exception:  # noqa: BLE001
                    pass

    def get_generate(self) -> str:
        """返回当前生效的生成 System Prompt; 空串表示使用内置默认。"""
        with self._lock:
            return self._data.get("generate_system", "") or ""

    def get_state(self) -> dict:
        with self._lock:
            return {
                "customized": bool(self._data.get("generate_system")),
                "prompt": self._data.get("generate_system", ""),
                "updated_at": self._data.get("updated_at", 0),
                "updated_by": self._data.get("updated_by", ""),
            }

    def set_generate(self, text: str, by: str = "") -> None:
        text = (text or "").strip()
        with self._lock:
            self._data["generate_system"] = text
            self._data["updated_at"] = time.time()
            self._data["updated_by"] = by or ""
        if self._path:
            try:
                Path(self._path).write_text(
                    json.dumps(self._data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:  # noqa: BLE001
                pass

    def reset(self, by: str = "") -> None:
        self.set_generate("", by)


_store: Optional[PromptStore] = None


def get_prompt_store() -> PromptStore:
    global _store
    if _store is None:
        _store = PromptStore(get_settings().prompt_store_path)
    return _store