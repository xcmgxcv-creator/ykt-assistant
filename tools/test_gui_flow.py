r"""离线验证主程序的跨线程刷新、数据目录切换和全部清理。

使用独立的临时数据与模拟客户端，不读取真实账号，也不访问雨课堂。
运行：.venv\Scripts\python.exe tools/test_gui_flow.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication, QDialog
import requests

from xxt import config, main
from xxt.rainclass.client import LoginResult
from xxt.rainclass.models import Activity, Course
from xxt.ui import dialogs
from xxt.ui.widget import TaskCard, TaskRow

COURSE_COUNT = 4
PER_COURSE_DELAY = 0.5        # 每门课假装要 0.5 秒，好让「同步中显示部分结果」可观测
DEADLINE_MS = int((time.time() + 72 * 3600) * 1000)


class FakeClient:
    """鸭子类型的雨课堂客户端：只实现 SyncService / main 真正会调用的方法。"""

    host = "https://demo.yuketang.cn"
    request_delay = 0.0

    def __init__(self, **kwargs):
        self.session = requests.Session()
        self.saved = 0

    def ensure_login(self):
        return LoginResult(True, "已登录", "同学")

    def fetch_profile(self):
        return {"name": "同学", "user_id": 1}

    def fetch_courses(self, latest_only=True):
        return [Course(classroom_id=88000 + i, name=f"课程 {i}", course_id=77000 + i)
                for i in range(COURSE_COUNT)]

    def fetch_activities(self, course):
        time.sleep(PER_COURSE_DELAY)
        return [Activity(activity_id=course.classroom_id,
                         classroom_id=course.classroom_id,
                         course_name=course.name,
                         title=f"作业 {course.name}",
                         type=2, end_ms=DEADLINE_MS)]

    def fetch_notices(self, courses, per_course=10):
        return []

    def fetch_system_messages(self):
        return []

    def fetch_upcoming_exams(self):
        return []

    def save_cookies(self):
        self.saved += 1


class IsolatedInstance:
    def __init__(self, *args):
        pass

    def create(self, *args):
        return True


def run() -> int:
    app = QApplication([])
    app.setStyle("Fusion")
    tmp = tempfile.TemporaryDirectory(prefix="ykt-app-flow-")
    root = Path(tmp.name)
    config._set_data_paths(root / "original")
    config.STORAGE_PATH = root / "storage.json"
    config.save()
    failures = []
    state = {}
    reload_threads = []
    original_reload = TaskCard.reload

    def checked_reload(card):
        reload_threads.append(QThread.currentThread() == app.thread())
        return original_reload(card)

    class TestSettings(dialogs.SettingsDialog):
        def exec(self):
            if state.get("action") == "migrate":
                self.data_directory.setText(str(root / "chosen"))
                return QDialog.DialogCode.Accepted
            self.reset_requested = True
            return QDialog.DialogCode.Rejected

    def check(fn):
        def wrapped():
            try:
                fn()
            except Exception as exc:
                failures.append(f"{fn.__name__}: {type(exc).__name__}: {exc}")
                app.quit()
        return wrapped

    @check
    def partial():
        card = next(w for w in app.topLevelWidgets() if isinstance(w, TaskCard))
        state["card"] = card
        count = len(card.findChildren(TaskRow))
        assert 0 < count < COURSE_COUNT, f"expected partial rows, got {count}"
        assert card.sync.busy
        print("PASS partial results visible while sync is running")

    @check
    def completed():
        card = state["card"]
        assert len(card.findChildren(TaskRow)) == COURSE_COUNT
        assert not card.sync.busy
        assert card.height() > 180
        assert all(reload_threads), "widget refreshed outside GUI thread"
        print("PASS completion refresh and sizing without reopening card")
        state["action"] = "migrate"
        card.settingsRequested.emit()

    @check
    def migrated():
        card = state["card"]
        assert config.DATA_DIR == root / "chosen"
        assert len(card.store.pending_assignments()) == COURSE_COUNT
        assert (root / "chosen" / "ykt.db").exists()
        print("PASS runtime data migration reopens backend")
        state["action"] = "clear"
        card.settingsRequested.emit()

    @check
    def cleared():
        card = state["card"]
        assert not card.store.pending_assignments()
        assert not config.COOKIE_PATH.exists()
        assert not config.CREDENTIAL_PATH.exists()
        assert not card.findChildren(TaskRow)
        print("PASS runtime clear resets store and live card")
        card.sync.stop()
        app.quit()

    QTimer.singleShot(1900, partial)
    QTimer.singleShot(3800, completed)
    QTimer.singleShot(4100, migrated)
    QTimer.singleShot(4400, cleared)
    QTimer.singleShot(8000, app.quit)
    with patch("PySide6.QtWidgets.QApplication", return_value=app), \
            patch("PySide6.QtCore.QSharedMemory", IsolatedInstance), \
            patch("xxt.main.RainClassClient", FakeClient), \
            patch.object(dialogs, "SettingsDialog", TestSettings), \
            patch.object(TaskCard, "reload", checked_reload), \
            patch("xxt.startup.is_enabled", return_value=False):
        main.main([])
    if "card" in state:
        state["card"].sync.stop()
        state["card"].store.close()
    import logging
    for handler in logging.getLogger().handlers[:]:
        handler.close()
        logging.getLogger().removeHandler(handler)
    tmp.cleanup()
    for failure in failures:
        print("FAIL", failure)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(run())
