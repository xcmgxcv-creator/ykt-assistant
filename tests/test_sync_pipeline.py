"""端到端（离线）：假客户端 → SyncService → Store → Notifier。

不联网、不起 GUI：用鸭子类型的假客户端替换 `RainClassClient`，
验证「取数 → 行映射 → 入库 → 算提醒 → 写 meta」整条链路，
并覆盖会话失效、单课失败、首轮不 announce 三个分支。

真正联网的部分（HTTP、cookie、扫码）由 `test_rainclass_client.py`
与 `test_rainclass_login.py` 用假 transport 覆盖。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from xxt.notify import Notifier
from xxt.rainclass.client import LoginResult
from xxt.rainclass.models import Activity, Course, Exam, Notice
from xxt.store import Store
from xxt.sync import SyncService

TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)


def _ms(hours: float) -> int:
    """相对现在的毫秒时间戳（雨课堂的 end 字段就是毫秒）。"""
    return int((NOW + timedelta(hours=hours)).timestamp() * 1000)


COURSE_A = Course(classroom_id=88001, name="示例课程甲", course_id=77001, term="202601")
COURSE_B = Course(classroom_id=88002, name="示例课程乙", course_id=77002, term="202601")


def _activities() -> dict[int, list[Activity]]:
    return {
        88001: [
            Activity(activity_id=11, classroom_id=88001, course_name="示例课程甲",
                     title="第一章 课后作业", type=2, end_ms=_ms(3)),
            Activity(activity_id=12, classroom_id=88001, course_name="示例课程甲",
                     title="已经过期的作业", type=2, end_ms=_ms(-5)),
            Activity(activity_id=13, classroom_id=88001, course_name="示例课程甲",
                     title="已经交掉的作业", type=2, end_ms=_ms(10), done=True),
            Activity(activity_id=14, classroom_id=88001, course_name="示例课程甲",
                     title="课件 1.1", type=14, end_ms=0),
        ],
        88002: [
            Activity(activity_id=21, classroom_id=88002, course_name="示例课程乙",
                     title="小作文", type=2, end_ms=_ms(30)),
        ],
    }


class FakeClient:
    """只实现 SyncService 真正会调用的那几个方法。"""

    host = "https://demo.yuketang.cn"

    def __init__(self, courses=(), activities=None, exams=(), notices=(),
                 login_ok=True, fail_courses=()):
        self.courses = list(courses)
        self.activities = dict(activities or {})
        self.exams = list(exams)
        self.notices = list(notices)
        self.login_ok = login_ok
        self.fail_courses = set(fail_courses)
        self.request_delay = 0.0
        self.saved = 0

    def ensure_login(self):
        if self.login_ok:
            return LoginResult(True, "已登录", "同学")
        return LoginResult(False, "会话已失效，请重新扫码")

    def fetch_profile(self):
        # 故意多带两个敏感字段：落库时应当被丢掉
        return {"name": "同学", "user_id": 12345,
                "phone_number": "13800000000", "school": "某大学"}

    def fetch_courses(self, latest_only=True):
        return list(self.courses)

    def fetch_activities(self, course):
        if course.classroom_id in self.fail_courses:
            raise RuntimeError("接口抽风")
        return list(self.activities.get(course.classroom_id, []))

    def fetch_notices(self, courses, per_course=10):
        return list(self.notices)

    def fetch_system_messages(self):
        return []

    def fetch_upcoming_exams(self):
        return list(self.exams)

    def save_cookies(self):
        self.saved += 1


def _build(tmp_path, client):
    store = Store(tmp_path / "t.db")
    toasts: list[tuple] = []
    notifier = Notifier(store, toast=lambda t, b, u: toasts.append((t, b, u)),
                        journal_path=tmp_path / "notify.json")
    return store, toasts, SyncService(store, client, notifier)


# --------------------------------------------------------------------- 正常链路


def test_full_pipeline_persists_rows_and_hides_profile(tmp_path):
    client = FakeClient(courses=[COURSE_A, COURSE_B], activities=_activities())
    store, toasts, service = _build(tmp_path, client)

    report = service.sync_once(announce_new=False)

    assert report.ok is True, report.errors
    assert report.courses == 2
    assert report.scanned == 5          # 4 + 1 条教学活动全部扫到
    assert len(report.new_assignments) == 5
    assert client.saved == 1

    # 未完成的都在库里（含已过期的那条，提醒层自己判断要不要响）
    pending = store.pending_assignments()
    assert len(pending) == 4
    assert len(store.all_assignments()) == 5
    # 排序：截止时间升序，没有截止时间的排最后
    assert [row["title"] for row in pending][-1] == "课件 1.1"
    assert pending[0]["title"] == "已经过期的作业"

    # 行映射落到了该落的列
    row = next(r for r in pending if r["title"] == "第一章 课后作业")
    assert row["course"] == "示例课程甲"
    assert row["clazzid"] == "88001"
    assert row["done"] == 0
    assert row["deadline_dt"]
    assert row["key"] == "ykt:88001:11"

    # 隐私：只留姓名与 id，手机号/学校不进库
    assert store.get_meta("profile") == {"name": "同学", "user_id": 12345}

    assert store.get_meta("sync_count") == 1
    assert store.get_meta("last_sync_ok") is True


def test_first_run_does_not_announce(tmp_path):
    client = FakeClient(courses=[COURSE_A], activities=_activities())
    store, toasts, service = _build(tmp_path, client)

    service.sync_once()                       # announce_new=None → 首轮视为 False
    assert not [t for t in toasts if "新作业" in t[0]]


def test_first_run_items_are_not_new_on_the_next_round(tmp_path):
    """首轮入库的作业只是本机第一次见到，下一轮不该当成「新作业」弹出来。

    `first_seen` 是入库时间，第二轮同步通常还在 20 分钟窗口内；
    没有 `suppress_new` 的话，用户装好程序后会被一堆老作业刷屏。
    """
    client = FakeClient(courses=[COURSE_A], activities=_activities())
    store, toasts, service = _build(tmp_path, client)

    service.sync_once(announce_new=False)     # 首轮：静默入库
    toasts.clear()
    service.sync_once()                       # 第二轮：announce=True
    assert not [t for t in toasts if "新作业" in t[0]]


def test_second_run_adds_nothing_new(tmp_path):
    client = FakeClient(courses=[COURSE_A], activities=_activities())
    store, _, service = _build(tmp_path, client)

    service.sync_once(announce_new=False)
    before = len(store.all_assignments())

    report = service.sync_once()
    assert report.ok is True
    assert report.new_assignments == []
    assert len(store.all_assignments()) == before      # 幂等
    assert store.get_meta("sync_count") == 2


def test_exams_and_notices_round_trip(tmp_path):
    exam = Exam(exam_id=6001, classroom_id=88001, course_name="示例课程甲",
                title="随堂测验", start_ms=_ms(2), duration_minutes=45)
    notice = Notice(notice_id="n-1", title="停课通知", content="本周三停课",
                    sender="教务处", publish_ms=_ms(-2), course_name="示例课程甲")
    client = FakeClient(courses=[COURSE_A], activities=_activities(),
                        exams=[exam], notices=[notice])
    store, _, service = _build(tmp_path, client)

    report = service.sync_once(announce_new=False)

    assert report.ok is True, report.errors
    assert len(store.unfinished_exams()) == 1
    assert store.unfinished_exams()[0]["name"] == "随堂测验"
    assert store.unfinished_exams()[0]["duration"] == "45"
    notices = store.notices(limit=10)
    assert [n["notice_id"] for n in notices] == ["n-1"]
    assert report.new_notices


# --------------------------------------------------------------------- 异常分支


def test_login_expired_marks_report_failed(tmp_path):
    client = FakeClient(courses=[COURSE_A], activities=_activities(), login_ok=False)
    store, _, service = _build(tmp_path, client)

    report = service.sync_once()

    assert report.ok is False
    assert "扫码" in report.errors[0]
    assert store.all_assignments() == []


def test_single_course_failure_does_not_abort_round(tmp_path):
    client = FakeClient(courses=[COURSE_A, COURSE_B], activities=_activities(),
                        fail_courses=[88002])
    store, _, service = _build(tmp_path, client)

    report = service.sync_once(announce_new=False)

    assert report.ok is True                 # 单课失败不算整轮失败
    assert report.courses == 2
    assert report.scanned == 4               # 只有甲课的数据
    assert any("示例课程乙" in e for e in report.errors)
