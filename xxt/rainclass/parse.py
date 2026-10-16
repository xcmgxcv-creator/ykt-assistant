"""雨课堂 JSON → 模型解析。

纯函数：不联网、不依赖 Qt、不读全局状态，fixture 直接喂进来就能测。

字段一律**容错取值**——上游多用 `null` 占位（`teacher`/`short_name`/`agent_id`
实测都可能是 `null`），雨课堂改版时先改这一层。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Sequence

from .models import Activity, Course, Exam, Notice

# 学期号 "0" 是雨课堂的特殊值（非正常学期），排序时永远排在最后
_TERM_SPECIAL = {"0", "", "none"}


# --------------------------------------------------------------------- 取值


def _int(value: Any, default: int | None = 0) -> int:
    """容错转 int：接受 `"123"` / `123.0` / `None`。"""
    if value is None or isinstance(value, bool):
        return default if default is not None else 0
    if isinstance(value, int):
        return value
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default if default is not None else 0


def _str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _listing(value: Any) -> list:
    return value if isinstance(value, list) else []


def _first(mapping: dict, *keys: str) -> Any:
    """按优先级取第一个非空字段（接口在不同版本用不同字段名）。"""
    for key in keys:
        if key in mapping:
            value = mapping[key]
            if value not in (None, "", [], {}):
                return value
    return None


# --------------------------------------------------------------------- 课程


def parse_courses(payload: dict) -> list[Course]:
    """`/v2/api/web/courses/list` → `Course` 列表。

    实测结构：`data.list[]`，每项含顶层 `name`/`term`/`classroom_id`，
    以及嵌套的 `course{}`（`id`/`university_id`/`name`）与 `teacher{}`（`name`）。
    """
    items = _listing((payload.get("data") or {}).get("list"))
    out: list[Course] = []
    for item in items:
        item = _mapping(item)
        classroom_id = _int(item.get("classroom_id"), default=0)
        if not classroom_id:
            continue
        course = _mapping(item.get("course"))
        teacher = _mapping(item.get("teacher"))
        out.append(Course(
            classroom_id=classroom_id,
            name=_str(_first(item, "name") or _first(course, "name")),
            course_id=_int(course.get("id"), default=0),
            term=_str(item.get("term")),
            university_id=_int(course.get("university_id"), default=0),
            platform_id=_int(course.get("platform"), default=3) or 3,
            teacher=_str(_first(teacher, "name")),
            raw=item,
        ))
    return out


def latest_term(courses: Sequence[Course]) -> str:
    """取最新学期号；全是特殊值时返回空串（调用方据此不做过滤）。"""
    terms = [c.term for c in courses if c.term and c.term.lower() not in _TERM_SPECIAL]
    if not terms:
        return ""
    return max(terms)


def filter_courses(courses: Sequence[Course], term: str | None = None,
                   latest_only: bool = True) -> list[Course]:
    """按学期筛选。

    `term` 为 None/"" 时：`latest_only=True` 只留最新学期，否则全部返回。
    指定了 `term` 就精确匹配（便于用户手动锁一个学期）。
    """
    if term:
        return [c for c in courses if c.term == term]
    if not latest_only:
        return list(courses)
    newest = latest_term(courses)
    if not newest:
        return list(courses)
    return [c for c in courses if c.term == newest]


# ------------------------------------------------------------------- 教学活动


def parse_activities(payload: dict, course: Course) -> list[Activity]:
    """`/v2/api/web/logs/learn/{classroom_id}` → `Activity` 列表。

    实测结构：`data.activities[]`，每项含 `type`/`id`/`title`/`end`/`count`
    与嵌套的 `view{depth,done}`。`end` 是**毫秒**截止时间。
    """
    items = _listing((payload.get("data") or {}).get("activities"))
    out: list[Activity] = []
    for item in items:
        item = _mapping(item)
        activity_id = _int(item.get("id"), default=0)
        if not activity_id:
            continue
        view = _mapping(item.get("view"))
        out.append(Activity(
            activity_id=activity_id,
            classroom_id=course.classroom_id,
            course_name=course.name,
            title=_str(item.get("title")),
            type=_int(item.get("type"), default=0),
            end_ms=_int(_first(item, "end", "end_time", "deadline"), default=0),
            count=_int(item.get("count"), default=0),
            depth=_int(view.get("depth"), default=0),
            done=bool(view.get("done")),
            is_finished=bool(item.get("is_finished")),
            attend_status=_int(item.get("attend_status"), default=0),
            raw=item,
        ))
    return out


def pending_homework(activities: Iterable[Activity],
                     now: datetime | None = None) -> list[Activity]:
    """待办作业：`type=2` 且未完成且未过期（无截止时间视为待办）。

    排序：有截止时间的按时间升序在前，无截止时间的垫底。
    `now` 显式传入时便于测试固定时钟。
    """
    from .models import TYPE_HOMEWORK

    items = [a for a in activities if a.type == TYPE_HOMEWORK and a.is_pending(now)]
    items.sort(key=lambda a: (a.deadline is None, a.end_ms))
    return items


# --------------------------------------------------------------------- 公告


def parse_announcements(payload: dict, course: Course) -> list[Notice]:
    """`/v/discussion/v2/announcements/` → `Notice` 列表。

    实测外层是 `data{status,message,data[],has_old_notice}` —— 公告数组**嵌在
    `data.data` 里**（不是 `data.list`），容易看错。
    """
    outer = _mapping(payload.get("data"))
    items = _listing(_first(outer, "data", "list", "announcements"))
    out: list[Notice] = []
    for item in items:
        item = _mapping(item)
        notice_id = _str(_first(item, "id", "notice_id", "announcement_id"))
        if not notice_id:
            continue
        out.append(Notice(
            notice_id=notice_id,
            title=_str(_first(item, "title", "name")),
            content=_str(_first(item, "content", "body", "text")),
            sender=_str(_first(item, "sender", "author", "user_name", "creator")),
            publish_ms=_int(_first(item, "publish_time", "create_time", "created_at",
                                   "time", "publish_time_ts"), default=0),
            is_read=bool(_first(item, "is_read", "isread", "read") or False),
            course_name=course.name,
            raw=item,
        ))
    return out


def parse_system_messages(payload: dict) -> list[Notice]:
    """`/c27/online_courseware/ykt/system_message/` → `Notice` 列表。

    实测结构：`data{count,results[],jump_link,has_message}`。
    """
    outer = _mapping(payload.get("data"))
    items = _listing(_first(outer, "results", "data", "list"))
    out: list[Notice] = []
    for item in items:
        item = _mapping(item)
        notice_id = _str(_first(item, "id", "message_id"))
        if not notice_id:
            continue
        out.append(Notice(
            notice_id=f"sys:{notice_id}",
            title=_str(_first(item, "title", "name")),
            content=_str(_first(item, "content", "body", "text")),
            sender="雨课堂",
            publish_ms=_int(_first(item, "publish_time", "create_time", "time"),
                            default=0),
            url=_str(_first(item, "jump_link", "link", "url")),
            raw=item,
        ))
    return out


# --------------------------------------------------------------------- 考试


def parse_upcoming_exams(payload: dict) -> list[Exam]:
    """`/api/v3/classroom/on-lesson-upcoming-exam` → `Exam` 列表。

    这是**课中**即将开始的考试，不是课后考试清单（雨课堂学生端没有后者，
    见 DESIGN.md §1.8）。接口没考试时返回空数组，属正常情况。
    """
    outer = _mapping(payload.get("data")) or payload
    items = _listing(_first(outer, "upcomingExam", "upcoming_exam", "exams", "list"))
    out: list[Exam] = []
    for item in items:
        item = _mapping(item)
        exam_id = _first(item, "exam_id", "id", "examId")
        if exam_id in (None, ""):
            continue
        out.append(Exam(
            exam_id=exam_id,
            classroom_id=_int(_first(item, "classroom_id", "classroomId"), default=0),
            course_name=_str(_first(item, "course_name", "courseName", "name")),
            title=_str(_first(item, "title", "name", "exam_name")),
            start_ms=_int(_first(item, "start_time", "startTime", "start_time_ts"),
                          default=0),
            duration_minutes=_int(_first(item, "duration", "duration_minutes",
                                         "limit_time"), default=0),
            raw=item,
        ))
    return out
