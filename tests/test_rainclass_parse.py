"""rainclass 解析层 / 端点层离线测试。

**零第三方依赖**（不需要 requests、不需要 Qt），fixture 全部脱敏。
固定时钟 `NOW`，因此断言不随系统时间漂移。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from xxt.rainclass import endpoints as ep
from xxt.rainclass import parse as rp
from xxt.rainclass.models import (KIND_COURSEWARE, KIND_HOMEWORK, KIND_OTHER,
                                  TYPE_COURSEWARE, TYPE_HOMEWORK, Activity,
                                  Course, ms_to_dt)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: 固定"现在"：2026-10-15 12:00
NOW = datetime(2026, 10, 15, 12, 0, 0)
#: fixture 里的毫秒时间戳含义
TS_PENDING = 1792425600000    # 2026-10-19 → 未来
TS_OVERDUE = 1600000000000    # 2020-09-13 → 过去
TS_FAR = 1900000000000        # 2030 年 → 未来


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def sample_course(classroom_id: int = 88880001, name: str = "示例课程A") -> Course:
    return Course(classroom_id=classroom_id, name=name, course_id=77770001,
                  term="202601", university_id=9999)


# --------------------------------------------------------------------- 端点


class TestEndpoints:
    def test_host_normalization(self):
        assert ep.normalize_host(None) == ep.DEFAULT_HOST
        assert ep.normalize_host("") == ep.DEFAULT_HOST
        assert ep.normalize_host("yuketang.cn") == "https://yuketang.cn"
        assert ep.normalize_host("https://x.example/") == "https://x.example"
        assert ep.normalize_host("http://changjiang.yuketang.cn") == \
            "http://changjiang.yuketang.cn"

    def test_host_of(self):
        assert ep.host_of("https://a.b/c?d=1") == "https://a.b"
        assert ep.host_of("not-a-url") == ""

    def test_required_header(self):
        # 实测：/mooc-api/ /c27/ /v/exam/ 缺这个头会被安全校验拦下
        assert ep.REQUIRED_HEADERS == {"xtbz": "ykt"}

    def test_learn_logs_url(self):
        url = ep.learn_logs("https://www.yuketang.cn", 88880001)
        assert url.startswith("https://www.yuketang.cn/v2/api/web/logs/learn/88880001?")
        assert "offset=50" in url
        assert "actype=-1" in url

    def test_courses_list_url(self):
        url = ep.courses_list("yuketang.cn", ts_ms=1700000000000)
        assert url.startswith("https://yuketang.cn/v2/api/web/courses/list?")
        assert "identity=2" in url
        assert "ts=1700000000000" in url

    def test_course_page_carries_classroom_id(self):
        url = ep.course_page("https://www.yuketang.cn", 88880001,
                             university_id=9999, platform_id=3)
        assert "/v2/web/studentLog/88880001?" in url
        assert "classroom_id=88880001" in url
        assert "tab=unfinished" in url

    def test_custom_host_propagates(self):
        url = ep.learn_logs("https://changjiang.yuketang.cn", 1)
        assert url.startswith("https://changjiang.yuketang.cn/")


# --------------------------------------------------------------------- 模型


class TestModels:
    def test_ms_to_dt_edge_cases(self):
        assert ms_to_dt(None) is None
        assert ms_to_dt(0) is None           # 0 = 无截止时间
        assert ms_to_dt(-1) is None          # 负值同样视为无效
        assert isinstance(ms_to_dt(TS_PENDING), datetime)

    def test_kind_and_progress(self):
        hw = Activity(activity_id=1, classroom_id=1, course_name="c", title="t",
                      type=TYPE_HOMEWORK, count=5, depth=2)
        cw = Activity(activity_id=2, classroom_id=1, course_name="c", title="t",
                      type=TYPE_COURSEWARE)
        other = Activity(activity_id=3, classroom_id=1, course_name="c", title="t",
                         type=99)
        assert hw.kind == KIND_HOMEWORK and hw.is_homework
        assert cw.kind == KIND_COURSEWARE
        assert other.kind == KIND_OTHER
        assert hw.progress == "2/5"
        assert cw.progress == ""

    def test_pending_requires_not_done_and_not_expired(self):
        base = dict(classroom_id=1, course_name="c", title="t", type=TYPE_HOMEWORK)
        pending = Activity(activity_id=1, end_ms=TS_PENDING, **base)
        overdue = Activity(activity_id=2, end_ms=TS_OVERDUE, **base)
        done = Activity(activity_id=3, end_ms=TS_PENDING, done=True, **base)
        finished = Activity(activity_id=4, end_ms=TS_PENDING, is_finished=True, **base)
        no_deadline = Activity(activity_id=5, end_ms=0, **base)

        assert pending.is_pending(NOW) and not pending.is_overdue(NOW)
        assert overdue.is_overdue(NOW) and not overdue.is_pending(NOW)
        assert not done.is_pending(NOW) and not done.is_overdue(NOW)
        assert not finished.is_pending(NOW)
        # 无截止时间视为"未完成且不紧急"→ 仍算待办
        assert no_deadline.is_pending(NOW)
        assert not no_deadline.is_overdue(NOW)

    def test_keys_are_stable(self):
        a = Activity(activity_id=7, classroom_id=42, course_name="c", title="t")
        assert a.key == "ykt:42:7"
        assert sample_course().key == "ykt:course:88880001"


# --------------------------------------------------------------------- 课程


class TestParseCourses:
    def test_parses_and_skips_dirty_rows(self):
        courses = rp.parse_courses(load("courses_list.json"))
        assert [c.classroom_id for c in courses] == [88880001, 88880002, 88880003]

        first = courses[0]
        assert first.name == "示例课程A"
        assert first.course_id == 77770001
        assert first.term == "202601"          # 接口给的是数字，模型统一为 str
        assert first.university_id == 9999
        assert first.teacher == "教师甲"

    def test_null_teacher_does_not_crash(self):
        courses = rp.parse_courses(load("courses_list.json"))
        assert courses[2].teacher == ""

    def test_latest_term_ignores_special_zero(self):
        courses = rp.parse_courses(load("courses_list.json"))
        assert rp.latest_term(courses) == "202601"

    def test_filter_courses(self):
        courses = rp.parse_courses(load("courses_list.json"))
        assert [c.classroom_id for c in rp.filter_courses(courses)] == [88880001]
        assert [c.classroom_id for c in rp.filter_courses(courses, "202502")] == \
            [88880002]
        assert len(rp.filter_courses(courses, latest_only=False)) == 3

    def test_empty_payload(self):
        assert rp.parse_courses({}) == []
        assert rp.parse_courses({"data": {"list": None}}) == []
        assert rp.latest_term([]) == ""


# --------------------------------------------------------------- 教学活动


class TestParseActivities:
    def test_all_fields(self):
        acts = rp.parse_activities(load("learn_logs.json"), sample_course())
        assert len(acts) == 6
        first = acts[0]
        assert first.activity_id == 1001
        assert first.title == "第1章 课后作业(3)"
        assert first.type == TYPE_HOMEWORK
        assert first.end_ms == TS_PENDING
        assert first.count == 3 and first.depth == 0
        assert first.done is False
        assert first.course_name == "示例课程A"
        assert first.classroom_id == 88880001

    def test_completed_flag_from_nested_view(self):
        acts = {a.activity_id: a for a in
                rp.parse_activities(load("learn_logs.json"), sample_course())}
        assert acts[1002].done is True
        assert acts[1002].depth == 5
        assert acts[1003].done is False

    def test_pending_homework_filters_and_sorts(self):
        acts = rp.parse_activities(load("learn_logs.json"), sample_course())
        pending = rp.pending_homework(acts, NOW)
        # 1001 未完成未过期；1005 无截止时间（垫底）；1002 已完成；1003 已过期；
        # 1004/1006 类型不是作业
        assert [a.activity_id for a in pending] == [1001, 1005]

    def test_malformed_rows_are_skipped(self):
        payload = {"data": {"activities": [
            {"id": 0, "type": 2},           # 无 id
            "not-a-dict",                   # 脏类型
            {"id": 55, "type": 2, "title": "ok", "end": TS_FAR, "view": None},
        ]}}
        acts = rp.parse_activities(payload, sample_course())
        assert [a.activity_id for a in acts] == [55]
        assert acts[0].done is False
        assert acts[0].depth == 0

    def test_empty_payload(self):
        assert rp.parse_activities({}, sample_course()) == []
        assert rp.pending_homework([], NOW) == []


# --------------------------------------------------------------------- 公告


class TestParseNotices:
    def test_announcements_nested_under_data_data(self):
        notices = rp.parse_announcements(load("announcements.json"), sample_course())
        assert [n.notice_id for n in notices] == ["9001", "9002"]
        first = notices[0]
        assert first.title == "关于课程考核方式的通知"
        assert first.sender == "教师甲"
        assert first.publish_ms == 1791000000000
        assert first.is_read is False
        assert first.course_name == "示例课程A"
        assert notices[1].is_read is True

    def test_empty_announcements(self):
        payload = {"data": {"status": 1, "message": "ok", "data": [],
                            "has_old_notice": False}}
        assert rp.parse_announcements(payload, sample_course()) == []

    def test_system_messages(self):
        notices = rp.parse_system_messages(load("system_message.json"))
        assert len(notices) == 1
        assert notices[0].notice_id == "sys:7001"
        assert notices[0].sender == "雨课堂"
        assert notices[0].url == "https://example.invalid/help?list=76"


# --------------------------------------------------------------------- 考试


class TestParseExams:
    def test_upcoming_exam(self):
        exams = rp.parse_upcoming_exams(load("upcoming_exam.json"))
        assert len(exams) == 1
        assert exams[0].exam_id == 6001
        assert exams[0].title == "随堂测验"
        assert exams[0].start_ms == TS_FAR
        assert exams[0].duration_minutes == 30
        assert exams[0].key == "ykt:exam:88880001:6001"

    def test_empty_is_normal(self):
        # 学生端没有课后考试清单接口，空数组是常态而非异常
        assert rp.parse_upcoming_exams(load("upcoming_exam_empty.json")) == []
        assert rp.parse_upcoming_exams({}) == []
