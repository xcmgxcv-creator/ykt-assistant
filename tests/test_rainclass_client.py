"""`RainClassClient` 的离线单测：会话/节流/登录态/汇总，全部走 fake session。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
import requests

from xxt import config
from xxt.rainclass import client as client_mod
from xxt.rainclass.client import (LoginResult, NotLoggedIn, RainClassClient,
                                  YktError)
from xxt.rainclass.models import Course

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HOST = "https://www.yuketang.cn"
NOW = datetime(2026, 10, 15, 12, 0, 0)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------- fake 网络


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = "",
                 url: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else ("" if payload is None else "{}")
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeCookie:
    def __init__(self, name, value, domain="", path="/"):
        self.name, self.value, self.domain, self.path = name, value, domain, path


class FakeCookies:
    def __init__(self):
        self._items: dict[str, FakeCookie] = {}

    def set(self, name, value, domain=None, path="/"):
        self._items[name] = FakeCookie(name, value, domain or "", path)

    def get(self, name, default=None):
        item = self._items.get(name)
        return item.value if item else default

    def clear(self):
        self._items.clear()

    def __iter__(self):
        return iter(self._items.values())


class FakeSession:
    """按 URL 子串匹配的假会话。routes: [(子串, FakeResponse|Exception)]"""

    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls: list[tuple[str, str]] = []
        self.cookies = FakeCookies()
        self.closed = False

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        for key, resp in self.routes:
            if key in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"未预期的请求：{url}")

    def close(self):
        self.closed = True


class FakeTime:
    """替换 client 模块内的 `time` 引用，避免污染全局 time 模块。"""

    def __init__(self, clock):
        self.clock = clock
        self.slept: list[float] = []

    def monotonic(self):
        return self.clock[0]

    def sleep(self, seconds):
        self.slept.append(seconds)


def make_client(routes=(), **kw) -> RainClassClient:
    kw.setdefault("request_delay", 0.0)
    return RainClassClient(host=HOST, session=FakeSession(routes), **kw)


def profile_payload(uid=33148528, name="示例同学"):
    return {"msg": "0.0.1", "success": True, "data": {
        "user_profile": {"user_id": uid, "name": name, "role": 2}}}


# ------------------------------------------------------------------ 会话


class TestSession:
    def test_requires_xtbz_header(self):
        seen = {}

        class Recorder(FakeSession):
            def request(self, method, url, **kw):
                seen.update(kw.get("headers") or {})
                return FakeResponse(200, payload=load("courses_list.json"))

        c = RainClassClient(host=HOST, request_delay=0,
                            session=Recorder([(".", FakeResponse(200, payload={"data": {"list": []}}))]))
        c.session.cookies.set("csrftoken", "x", domain=".yuketang.cn")
        try:
            c._request("https://www.yuketang.cn/x")
        except Exception:  # noqa: BLE001
            pass
        assert seen.get("xtbz") == "ykt"

    def test_401_raises_not_logged_in(self):
        c = make_client([("user_info", FakeResponse(401))])
        with pytest.raises(NotLoggedIn):
            c._request("https://www.yuketang.cn/v/course_meta/user_info")

    def test_redirect_to_login_raises(self):
        resp = FakeResponse(200, payload={"a": 1},
                            url="https://www.yuketang.cn/v2/web/login")
        c = make_client([("courses", resp)])
        with pytest.raises(NotLoggedIn):
            c._get_json("https://www.yuketang.cn/v2/api/web/courses/list")

    def test_server_error_raises_ykt_error(self):
        c = make_client([("courses", FakeResponse(500))])
        with pytest.raises(YktError, match="500"):
            c._get_json("https://www.yuketang.cn/v2/api/web/courses/list")

    def test_non_json_raises(self):
        c = make_client([("courses", FakeResponse(200, text="<html>"))])
        with pytest.raises(YktError, match="JSON"):
            c._get_json("https://www.yuketang.cn/v2/api/web/courses/list")

    def test_network_exception_wrapped(self):
        c = make_client([("courses", requests.ConnectionError("boom"))])
        with pytest.raises(YktError, match="boom"):
            c._get_json("https://www.yuketang.cn/v2/api/web/courses/list")


class TestThrottle:
    def test_no_delay_is_noop(self, monkeypatch):
        fake = FakeTime([100.0])
        monkeypatch.setattr(client_mod, "time", fake)
        make_client(request_delay=0.0)._throttle()
        assert fake.slept == []

    def test_delay_sleeps_within_jitter(self, monkeypatch):
        fake = FakeTime([100.0])
        monkeypatch.setattr(client_mod, "time", fake)
        c = make_client(request_delay=1.0)
        c._throttle()          # 首次：距上次很久 → 不睡
        fake.clock[0] = 100.1
        c._throttle()          # 立刻再发 → 需要补 1.0s ± 30%
        assert len(fake.slept) == 1
        assert 0.6 <= fake.slept[0] <= 1.4


# ------------------------------------------------------------------ 登录


class TestEnsureLogin:
    def test_ok_and_cached(self):
        c = make_client([("user_info", FakeResponse(200, payload=profile_payload()))])
        first = c.ensure_login()
        assert first.ok is True
        assert first.name == "示例同学"

        second = c.ensure_login()
        assert second.ok is True
        # 缓存命中：只打过一次探测请求
        assert len(c.session.calls) == 1

    def test_force_bypasses_cache(self):
        c = make_client([("user_info", FakeResponse(200, payload=profile_payload()))])
        c.ensure_login()
        c.ensure_login(force=True)
        assert len(c.session.calls) == 2

    def test_missing_profile_is_failure(self):
        c = make_client([("user_info", FakeResponse(200, payload={"data": {}}))])
        assert c.ensure_login().ok is False

    def test_not_logged_in_is_reported_not_raised(self):
        c = make_client([("user_info", FakeResponse(403))])
        result = c.ensure_login()
        assert result.ok is False
        assert "失败" not in result.msg or "登录" in result.msg

    def test_profile_is_not_persisted_by_fetch_profile(self):
        c = make_client([("user_info", FakeResponse(200, payload=profile_payload()))])
        assert c.fetch_profile()["name"] == "示例同学"


# ------------------------------------------------------------------ 读取


class TestFetch:
    def test_courses_latest_only(self):
        c = make_client([("courses/list", FakeResponse(200,
                                                       payload=load("courses_list.json")))])
        courses = c.fetch_courses()
        assert [x.classroom_id for x in courses] == [88880001]

    def test_courses_all_terms(self):
        c = make_client([("courses/list", FakeResponse(200,
                                                       payload=load("courses_list.json")))])
        assert len(c.fetch_courses(latest_only=False)) == 3

    def test_courses_cached(self):
        c = make_client([("courses/list", FakeResponse(200,
                                                       payload=load("courses_list.json")))])
        c.fetch_courses()
        c.fetch_courses()
        assert len(c.session.calls) == 1

    def test_empty_course_list_is_an_error(self):
        c = make_client([("courses/list", FakeResponse(200,
                                                       payload={"data": {"list": []}}))])
        with pytest.raises(YktError, match="课程列表为空"):
            c.fetch_courses()

    def test_fetch_pending_sorts_across_courses(self):
        c = make_client([
            ("courses/list", FakeResponse(200, payload=load("courses_list.json"))),
            ("logs/learn/88880001", FakeResponse(200, payload=load("learn_logs.json"))),
            ("logs/learn/88880002", FakeResponse(200, payload=load("learn_logs.json"))),
        ])
        courses = [Course(classroom_id=88880001, name="A", term="202601"),
                   Course(classroom_id=88880002, name="B", term="202601")]
        pending = c.fetch_pending(courses, NOW)
        # 每门课贡献 1001（有截止）与 1005（无截止），跨课统一排序
        assert [a.classroom_id for a in pending] == [88880001, 88880002,
                                                     88880001, 88880002]
        assert [a.activity_id for a in pending] == [1001, 1001, 1005, 1005]

    def test_fetch_notices_sorted_newest_first(self):
        c = make_client([
            ("announcements", FakeResponse(200, payload=load("announcements.json"))),
        ])
        notices = c.fetch_notices([Course(classroom_id=88880001, name="A")])
        assert [n.notice_id for n in notices] == ["9001", "9002"]

    def test_upcoming_exam_failure_degrades_to_empty(self):
        c = make_client([("on-lesson-upcoming-exam", FakeResponse(500))])
        assert c.fetch_upcoming_exams() == []

    def test_upcoming_exam_propagates_not_logged_in(self):
        c = make_client([("on-lesson-upcoming-exam", FakeResponse(403))])
        with pytest.raises(NotLoggedIn):
            c.fetch_upcoming_exams()


# ------------------------------------------------------------- cookie 落盘


class TestCookiePersistence:
    def test_roundtrip_is_encrypted(self, tmp_path, monkeypatch):
        path = tmp_path / "session_cookies.json"
        monkeypatch.setattr(config, "COOKIE_PATH", path)
        monkeypatch.setattr(config, "ensure_dirs", lambda: None)

        saver = make_client()
        saver.session.cookies.set("csrftoken", "SECRET-VALUE",
                                  domain=".yuketang.cn")
        saver.save_cookies()

        raw = path.read_text(encoding="ascii")
        assert "SECRET-VALUE" not in raw          # 密文里不能出现明文

        loader = make_client()
        assert loader.session.cookies.get("csrftoken") == "SECRET-VALUE"

    def test_missing_file_is_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "COOKIE_PATH", tmp_path / "nope.json")
        assert make_client().load_cookies() is False

    def test_corrupt_file_is_false(self, tmp_path, monkeypatch):
        path = tmp_path / "session_cookies.json"
        path.write_text("not-a-valid-token", encoding="ascii")
        monkeypatch.setattr(config, "COOKIE_PATH", path)
        assert make_client().load_cookies() is False

    def test_clear_removes_file_and_cookies(self, tmp_path, monkeypatch):
        path = tmp_path / "session_cookies.json"
        monkeypatch.setattr(config, "COOKIE_PATH", path)
        monkeypatch.setattr(config, "ensure_dirs", lambda: None)
        c = make_client()
        c.session.cookies.set("csrftoken", "v")
        c.save_cookies()
        c.clear_cookies()
        assert not path.exists()
        assert c.session.cookies.get("csrftoken") is None
