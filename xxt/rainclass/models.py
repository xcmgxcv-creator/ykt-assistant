"""雨课堂数据模型。

只有 dataclass 与纯计算属性，不含 HTTP、不含解析、不依赖 Qt——
因此测试里可以直接构造实例，不需要任何 fixture 之外的脚手架。

时间约定：接口给的都是**毫秒**时间戳；模型内部统一存 `int` 毫秒
（`*_ms` 后缀），需要 `datetime` 时走属性换算，避免在管道中途反复转型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# logs/learn 的 type 枚举（实测：2=作业/测验，14=课件/视频）
TYPE_HOMEWORK = 2
TYPE_COURSEWARE = 14

KIND_HOMEWORK = "homework"
KIND_COURSEWARE = "courseware"
KIND_OTHER = "other"


def ms_to_dt(ms: int | None) -> datetime | None:
    """毫秒时间戳 → 本地时区 datetime；0/None/越界一律返回 None。"""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class Course:
    """一个教学班（雨课堂的「课程」= 班级实例，带独立 classroom_id）。"""

    classroom_id: int
    name: str
    course_id: int = 0
    term: str = ""
    university_id: int = 0
    platform_id: int = 3
    teacher: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def key(self) -> str:
        return f"ykt:course:{self.classroom_id}"


@dataclass(frozen=True)
class Activity:
    """`logs/learn` 返回的一条教学活动。

    这是待办的核心载体：截止时间（`end_ms`）与完成状态（`done`）都在同一条记录里，
    不需要再拉详情页——这也是上游项目最容易坏的那一环被去掉的原因。
    """

    activity_id: int
    classroom_id: int
    course_name: str
    title: str
    type: int = 0
    end_ms: int = 0
    count: int = 0
    depth: int = 0
    done: bool = False
    is_finished: bool = False
    attend_status: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # ---------------------------------------------------------------- 分类

    @property
    def kind(self) -> str:
        if self.type == TYPE_HOMEWORK:
            return KIND_HOMEWORK
        if self.type == TYPE_COURSEWARE:
            return KIND_COURSEWARE
        return KIND_OTHER

    @property
    def is_homework(self) -> bool:
        return self.type == TYPE_HOMEWORK

    # ---------------------------------------------------------------- 时间

    @property
    def deadline(self) -> datetime | None:
        return ms_to_dt(self.end_ms)

    def is_pending(self, now: datetime | None = None) -> bool:
        """未完成、且截止时间还没过（或压根没有截止时间）。"""
        if self.done or self.is_finished:
            return False
        dl = self.deadline
        if dl is None:
            return True
        now = now or datetime.now()
        return dl >= now

    def is_overdue(self, now: datetime | None = None) -> bool:
        if self.done or self.is_finished:
            return False
        dl = self.deadline
        if dl is None:
            return False
        return dl < (now or datetime.now())

    # ---------------------------------------------------------------- 身份

    @property
    def key(self) -> str:
        """稳定去重键：同一教学班下的活动 id 唯一。"""
        return f"ykt:{self.classroom_id}:{self.activity_id}"

    @property
    def progress(self) -> str:
        if self.count <= 0:
            return ""
        return f"{self.depth}/{self.count}"


@dataclass(frozen=True)
class Exam:
    """课中即将开始的考试（`on-lesson-upcoming-exam`）。

    雨课堂学生端没有课后考试清单接口（见 DESIGN.md §1.8），
    因此本模型只在课堂上真有考试且接口返回时才产生实例。
    """

    exam_id: int | str
    classroom_id: int
    course_name: str
    title: str
    start_ms: int = 0
    duration_minutes: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def start_dt(self) -> datetime | None:
        return ms_to_dt(self.start_ms)

    @property
    def key(self) -> str:
        return f"ykt:exam:{self.classroom_id}:{self.exam_id}"


@dataclass(frozen=True)
class Notice:
    """公告 / 平台消息。"""

    notice_id: str
    title: str
    content: str = ""
    sender: str = ""
    publish_ms: int = 0
    is_read: bool = False
    course_name: str = ""
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def publish_dt(self) -> datetime | None:
        return ms_to_dt(self.publish_ms)

    @property
    def key(self) -> str:
        return f"ykt:notice:{self.notice_id}"
