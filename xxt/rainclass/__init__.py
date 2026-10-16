"""雨课堂数据层：本项目唯一包含平台细节的地方。

对外只暴露本包的模型与客户端；`sync`/`ui` 等上层不应出现任何 URL、字段名或
平台判断——雨课堂改版时只改这一层。

    from .rainclass import RainClassClient, Course, Activity

设计约束：
    * 全部请求都是只读查询（不交作业、不签到、不答题、不刷课）
    * 请求之间强制限速（默认 0.7s），远低于网页端自身 12s 轮询的量级
    * 本层不依赖 Qt，可离线单测

`parse` / `models` / `endpoints` 三个子模块是**零第三方依赖**的纯逻辑，
因此可以在只有标准库的环境里直接跑解析测试；`client` 才需要 requests，
所以它在下面用 PEP 562 延迟导入。
"""

from __future__ import annotations

from . import endpoints, models, parse
from .models import Activity, Course, Exam, Notice

__all__ = [
    "endpoints",
    "models",
    "parse",
    "Course",
    "Activity",
    "Exam",
    "Notice",
    "RainClassClient",
    "RainClassError",
    "LoginExpired",
    "LoginResult",
]

_LAZY = ("RainClassClient", "RainClassError", "LoginExpired", "LoginResult")


def __getattr__(name: str):
    """延迟导入需要 requests 的 client 模块（保持解析层的零依赖）。"""
    if name in _LAZY:
        from . import client

        return getattr(client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
