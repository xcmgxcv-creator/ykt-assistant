"""同步编排：拉取 -> 落库 -> 算提醒。

雨课堂版比上游简单得多：**截止时间与完成状态都在教学活动列表里**
（`GET /v2/api/web/logs/learn/{cid}` 的 `view.done` 与 `end`），
因此没有「详情页补全」这一环——上游最容易静默失效的部分被整段去掉了。

一次完整同步 = 1 次登录态探测 + 1 次课程列表 + 每门课 1 次教学活动
+ 1 次公告 + 1 次平台消息 + 1 次临近考试，串行限速。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import config
from .notify import Notifier
from .rainclass.client import (LoginResult, NotLoggedIn, RainClassClient,
                               YktError)
from .rainclass.models import Activity, Course, Notice
from .store import Store

log = logging.getLogger("xxt.sync")


@dataclass
class SyncReport:
    started_at: datetime
    finished_at: datetime | None = None
    ok: bool = True
    courses: int = 0
    scanned: int = 0
    new_assignments: list[dict] = field(default_factory=list)
    new_exams: list[dict] = field(default_factory=list)
    new_notices: list[dict] = field(default_factory=list)
    reminded: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        if not self.finished_at:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    def summary(self) -> str:
        when = self.finished_at or datetime.now()
        head = "同步成功" if self.ok else "同步失败"
        text = (f"{head}　{when:%H:%M:%S}　课程 {self.courses}　作业 {self.scanned}"
                f"　新增 {len(self.new_assignments)}　耗时 {self.duration:.0f}s")
        if self.reminded:
            text += f"　提醒 {self.reminded}"
        return text


class SyncService:
    def __init__(self, store: Store, client: RainClassClient, notifier: Notifier,
                 on_report=None, on_progress=None):
        self.store = store
        self.client = client
        self.notifier = notifier
        self.on_report = on_report
        self.on_progress = on_progress
        self.last_report: SyncReport | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._running = threading.Lock()
        self.next_run_at: datetime | None = None

    # -------------------------------------------------- 登录

    def ensure_login(self) -> LoginResult:
        """检查会话是否还有效（登录本身走扫码对话框，见 `ui.dialogs.LoginDialog`）。"""
        return self.client.ensure_login()

    def _progress(self, text: str) -> None:
        if self.on_progress:
            self.on_progress(text)

    # -------------------------------------------------- 单次同步

    def sync_once(self, announce_new: bool | None = None) -> SyncReport:
        report = SyncReport(started_at=datetime.now())
        if not self._running.acquire(blocking=False):
            report.ok = False
            report.errors.append("已有同步在进行中")
            report.finished_at = datetime.now()
            return report
        try:
            self._do_sync(report, announce_new)
        except YktError as exc:
            report.ok = False
            report.errors.append(str(exc))
        except Exception as exc:  # pragma: no cover - 兜底
            log.exception("同步异常")
            report.ok = False
            report.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            report.finished_at = datetime.now()
            self.last_report = report
            self._persist(report)
            self._running.release()
            if report.ok:
                log.info("%s", report.summary())
            else:
                log.warning("同步失败：%s", "；".join(report.errors[:2]) or "未知原因")
            if self.on_report:
                try:
                    self.on_report(report)
                except Exception:  # pragma: no cover
                    log.exception("同步回调异常")
        return report

    def _do_sync(self, report: SyncReport, announce_new: bool | None) -> None:
        self._progress("正在验证登录状态…")
        login = self.client.ensure_login()
        if not login.ok:
            raise NotLoggedIn(login.msg)

        cfg = config.load()
        self.client.request_delay = float(cfg["sync"]["request_delay"])
        first_run = not self.store.get_meta("sync_count")

        # 只保留界面要展示的字段：本机库里不写手机号 / 学号 / 学院
        profile = self.client.fetch_profile()
        self.store.set_meta("profile", {"name": profile.get("name") or "",
                                        "user_id": profile.get("user_id") or 0})

        self._progress("正在获取课程列表…")
        courses = self.client.fetch_courses()
        report.courses = len(courses)
        if not courses:
            report.notes.append("未获取到任何课程")

        for index, course in enumerate(courses, 1):
            if self._stop.is_set():
                raise YktError("同步已停止")
            self._progress(f"正在同步课程 {index}/{len(courses)} · {course.name}")
            try:
                activities = self.client.fetch_activities(course)
            except YktError as exc:
                report.errors.append(f"{course.name}：{exc}")
                continue
            except Exception as exc:  # pragma: no cover - 单课失败不中断整轮
                report.errors.append(f"{course.name}：{type(exc).__name__} {exc}")
                continue
            rows = [self._activity_row(course, activity) for activity in activities]
            report.scanned += len(rows)
            diff = self.store.upsert_assignments(rows)
            for key in diff["new"]:
                row = self._assignment(key)
                if row:
                    report.new_assignments.append(row)
            self._progress(f"已同步 {index}/{len(courses)} 门课程 · 作业 {report.scanned} 项")

        if self._stop.is_set():
            raise YktError("同步已停止")
        self._progress("正在同步考试和公告…")
        self._sync_exams(report)
        if self._stop.is_set():
            raise YktError("同步已停止")
        self._sync_notices(report, courses, cfg)

        if self._stop.is_set():
            raise YktError("同步已停止")
        announce = (not first_run) if announce_new is None else announce_new
        if first_run:
            # 首轮入库的作业/考试只是本机第一次见到，不是真的「新发布」；
            # 标记成已通知，免得下一轮同步在 20 分钟窗口内把它们全弹出来。
            self.notifier.suppress_new([row["key"] for row in report.new_assignments],
                                       [row["key"] for row in report.new_exams])
        reminders = self.notifier.scan(announce_new=announce)
        report.reminded = self.notifier.flush(reminders)
        self.store.set_meta("sync_count", int(self.store.get_meta("sync_count", 0)) + 1)
        # 会话 cookie 已经在扫码登录时落盘；这里再存一次，覆盖服务端刷新的值
        self.client.save_cookies()

    # -------------------------------------------------- 行映射

    def _activity_row(self, course: Course, activity: Activity) -> dict:
        """一条教学活动 → `assignments` 表的一行（含截止时间，无需二次请求）。"""
        done = bool(activity.done or activity.is_finished)
        deadline = activity.deadline
        return {
            "key": activity.key,
            "course": course.name,
            "courseid": str(course.course_id),
            "clazzid": str(course.classroom_id),
            "title": activity.title,
            "status": "已完成" if done else "未完成",
            "url": f"{self.client.host}/v2/web/studentLog/{course.classroom_id}",
            "deadline": deadline.strftime("%Y-%m-%d %H:%M") if deadline else None,
            "deadline_dt": deadline.isoformat(timespec="minutes") if deadline else None,
            "done": done,
        }

    def _sync_exams(self, report: SyncReport) -> None:
        try:
            exams = self.client.fetch_upcoming_exams()
        except Exception as exc:
            report.errors.append(f"考试列表：{exc}")
            return
        rows = []
        for exam in exams:
            start = exam.start_dt
            rows.append({
                "key": exam.key,
                "name": exam.title,
                "course": exam.course_name,
                "exam_time": start.strftime("%Y-%m-%d %H:%M") if start else None,
                "start_dt": start.isoformat(timespec="minutes") if start else None,
                "duration": str(exam.duration_minutes) if exam.duration_minutes else None,
                "exam_status": "",
                "answer_status": "未完成",
            })
        diff = self.store.upsert_exams(rows)
        for key in diff["new"]:
            for row in self.store.unfinished_exams():
                if row["key"] == key:
                    report.new_exams.append(row)
                    break

    def _sync_notices(self, report: SyncReport, courses: list[Course],
                      cfg: dict) -> None:
        ncfg = cfg.get("notices", {})
        if not ncfg.get("enabled", True):
            return
        limit = int(ncfg.get("limit", 30))
        found: list[Notice] = []
        try:
            found.extend(self.client.fetch_notices(courses, per_course=10))
        except Exception as exc:
            report.errors.append(f"课程公告：{exc}")
        try:
            found.extend(self.client.fetch_system_messages())
        except Exception as exc:
            report.errors.append(f"平台消息：{exc}")
        rows = [self._notice_row(item) for item in found[:limit]]
        report.new_notices = self.store.upsert_notices(rows)

    @staticmethod
    def _notice_row(notice: Notice) -> dict:
        published = notice.publish_dt
        return {
            "notice_id": notice.notice_id,
            "title": notice.title,
            "content": notice.content,
            "sender": notice.sender,
            "publish_time": (published.isoformat(timespec="minutes")
                             if published else ""),
            "isread": notice.is_read,
        }

    def _assignment(self, key: str) -> dict | None:
        rows = self.store._query("SELECT * FROM assignments WHERE key=?", (key,))
        return dict(rows[0]) if rows else None

    def _persist(self, report: SyncReport) -> None:
        self.store.set_meta("last_sync_at", (report.finished_at or datetime.now()
                                            ).isoformat(timespec="seconds"))
        self.store.set_meta("last_sync_ok", report.ok)
        self.store.set_meta("last_sync_summary", report.summary())
        self.store.set_meta("last_errors", report.errors[:5])

    # -------------------------------------------------- 后台循环

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ykt-sync",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def trigger(self) -> None:
        """立刻唤醒一次同步（手动刷新按钮用）。"""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            cfg = config.load()
            interval = max(5, int(cfg.get("sync", {}).get("interval_minutes", 30)))
            jitter = max(0, int(cfg.get("sync", {}).get("jitter_seconds", 90)))
            wait = interval * 60 + random.randint(0, jitter)
            if self.last_report is None:
                wait = 1  # 启动后先跑一次
            self.next_run_at = datetime.fromtimestamp(time.time() + wait)
            if self._wake.wait(timeout=wait):
                self._wake.clear()
                if self._stop.is_set():
                    break
            if self._stop.is_set():
                break
            self.sync_once()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def busy(self) -> bool:
        """当前是否有同步任务在跑（UI 用来禁用刷新按钮）。"""
        return self._running.locked()
