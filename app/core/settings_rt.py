"""可覆盖的 Settings 视图: 在静态配置之上叠加运行时动态覆盖。

`effective_settings()` 返回一个与 Settings 同接口的轻量对象, 供
retrieval / rerank / graph nodes 读取动态检索参数, 而无需逐字段改写
`settings.xxx`。读取时: 动态覆盖值优先, 否则回退静态默认值。

不复制静态 Settings 的全部字段, 仅按需透明转发 getattr。
"""

from __future__ import annotations

from typing import Any

from app.config import Settings, get_settings
from app.core.runtime_config import get_runtime_config


class _EffectiveSettings:
    """动态可覆盖的 Settings 视图。"""

    def __init__(self, base: Settings) -> None:
        self._base = base
        self._rt = get_runtime_config()

    def is_overridden(self, name: str) -> bool:
        return self._rt.is_overridden(name)

    def __getattr__(self, name: str) -> Any:
        rt = getattr(self, "_rt")
        if rt.is_overridden(name):
            return rt.get(name)
        return getattr(self._base, name)


_cache: _EffectiveSettings | None = None


def effective_settings() -> _EffectiveSettings:
    """返回动态覆盖优先的配置视图(进程级单例, 读取实时生效)。"""
    global _cache
    if _cache is None:
        _cache = _EffectiveSettings(get_settings())
    return _cache