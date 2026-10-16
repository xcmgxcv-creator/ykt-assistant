"""雨课堂只读客户端。

**只读语义**：本模块只发查询请求（`GET` 与登录用的 `POST`），不提交作业、
不参加考试、不签到、不发弹幕。请求之间强制限速。

与上游超星版的差异（决定了它更简单）：
    * 返回 **JSON**，无需 AES 解密、无需从 HTML 里抠签名
    * 鉴权 = Cookie 会话 + 自定义头 `xtbz: ykt`
    * 截止时间就在列表接口里，**不需要详情页补全**（上游最脆弱的一环）

平台细节只在这一层 + `endpoints.py` 里；上层（`sync`/UI/提醒/存储）不感知雨课堂。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

from .. import config, crypto
from . import endpoints as ep
from . import parse as rp
from .login import LoginError, QrLoginSession, QrState
from .models import Activity, Course, Exam, Notice

log = logging.getLogger("xxt.rainclass.client")

TIMEOUT = 20.0

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

#: 登录态检查结果缓存多久（避免每轮同步都多打一次探测请求）
LOGIN_CHECK_TTL = 300.0
#: 课程列表缓存多久（同步一轮只取一次）
COURSES_TTL = 120.0


class YktError(Exception):
    """雨课堂采集层通用异常。"""


class NotLoggedIn(YktError):
    """Cookie 会话失效，需要用户重新扫码。"""


@dataclass
class LoginResult:
    ok: bool
    msg: str = ""
    name: str = ""


class RainClassClient:
    """雨课堂只读客户端。

    `host` 留空时读配置里的 `platform.host`，因此校级域名
    （如 `https://changjiang.yuketang.cn`）只需改配置。
    """

    def __init__(self, request_delay: float = 0.7, timeout: float = TIMEOUT,
                 host: str | None = None, *, session: Any = None) -> None:
        self.host = ep.normalize_host(host or self._config_host())
        self.timeout = timeout
        self.request_delay = max(0.0, request_delay)
        self._last_request_at = 0.0
        self._login_ok: bool | None = None
        self._login_checked_at = 0.0
        self._profile: dict[str, Any] = {}
        self._courses_cache: list[Course] | None = None
        self._courses_at = 0.0
        self.session = session if session is not None else self._build_session()
        self.load_cookies()

    @staticmethod
    def _config_host() -> str:
        try:
            return (config.load().get("platform") or {}).get("host") or ep.DEFAULT_HOST
        except Exception:  # noqa: BLE001 - 配置损坏不应阻止启动
            return ep.DEFAULT_HOST

    # ------------------------------------------------------------ 会话

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": ep.index_page(self.host),
        })
        retry = Retry(total=2, backoff_factor=0.6,
                      status_forcelist=(429, 502, 503, 504),
                      allowed_methods=frozenset(["GET", "POST"]))
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=4)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            pass

    # -------------------------------------------------------- Cookie 落盘

    def save_cookies(self) -> None:
        """把会话 Cookie 用 DPAPI 加密后写入 `data/session_cookies.json`。"""
        try:
            config.ensure_dirs()
            payload = json.dumps(
                [{"name": c.name, "value": c.value, "domain": c.domain,
                  "path": c.path}
                 for c in self.session.cookies],
                ensure_ascii=False)
            token = crypto.protect(payload)
            tmp = config.COOKIE_PATH.with_suffix(".json.tmp")
            tmp.write_text(token, encoding="ascii")
            os.replace(tmp, config.COOKIE_PATH)
        except Exception as exc:  # noqa: BLE001 - 存不下不该让同步崩掉
            log.warning("保存会话失败：%s", exc)

    def load_cookies(self) -> bool:
        """读回加密的会话 Cookie；没有或解不开都返回 False。"""
        path = config.COOKIE_PATH
        if not path.exists():
            return False
        try:
            token = path.read_text(encoding="ascii").strip()
            entries = json.loads(crypto.unprotect(token))
        except Exception as exc:  # noqa: BLE001
            log.info("会话文件不可用（%s），按未登录处理", exc)
            return False
        for item in entries or []:
            try:
                self.session.cookies.set(item["name"], item["value"],
                                         domain=item.get("domain") or None,
                                         path=item.get("path") or "/")
            except Exception:  # noqa: BLE001
                continue
        return True

    def clear_cookies(self) -> None:
        try:
            config.COOKIE_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            self.session.cookies.clear()
        except Exception:  # noqa: BLE001
            pass
        self._login_ok = None
        self._profile = {}

    # ------------------------------------------------------------ 请求

    def _throttle(self) -> None:
        if self.request_delay <= 0:
            return
        gap = time.monotonic() - self._last_request_at
        wait = self.request_delay * (1 + random.uniform(-0.3, 0.3)) - gap
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _request(self, url: str, *, method: str = "GET", **kw) -> requests.Response:
        self._throttle()
        headers = dict(ep.REQUIRED_HEADERS)
        headers["Accept"] = "application/json, text/plain, */*"
        try:
            resp = self.session.request(method, url, headers=headers,
                                        timeout=self.timeout, **kw)
        except requests.RequestException as exc:
            raise YktError(f"请求失败：{exc}") from exc

        if resp.status_code in (401, 403):
            self._login_ok = False
            raise NotLoggedIn(f"雨课堂返回 HTTP {resp.status_code}，会话可能已失效")
        if self._redirected_to_login(resp):
            self._login_ok = False
            raise NotLoggedIn("雨课堂要求重新登录")
        if resp.status_code >= 400:
            raise YktError(f"雨课堂返回 HTTP {resp.status_code}：{url}")
        return resp

    @staticmethod
    def _redirected_to_login(resp: requests.Response) -> bool:
        final = getattr(resp, "url", "") or ""
        return "/v2/web/login" in final or "passport" in final

    def _get_json(self, url: str) -> dict[str, Any]:
        resp = self._request(url)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise YktError(f"雨课堂返回的不是 JSON：{url}") from exc
        if not isinstance(payload, dict):
            raise YktError(f"雨课堂返回结构异常：{url}")
        return payload

    # ------------------------------------------------------------ 登录

    def ensure_login(self, force: bool = False) -> LoginResult:
        """检查 Cookie 是否仍然有效（结果缓存 5 分钟）。"""
        now = time.monotonic()
        if not force and self._login_ok is not None and \
                (now - self._login_checked_at) < LOGIN_CHECK_TTL:
            return LoginResult(self._login_ok,
                               "已登录" if self._login_ok else "登录态已失效",
                               self._display_name())

        url = ep.user_info_url(self.host)
        try:
            payload = self._get_json(url)
        except NotLoggedIn as exc:
            return LoginResult(False, str(exc))
        except YktError as exc:
            return LoginResult(False, str(exc))

        profile = ((payload.get("data") or {}).get("user_profile") or {})
        ok = bool(profile.get("user_id"))
        self._login_ok = ok
        self._login_checked_at = now
        if ok:
            self._profile = profile
        return LoginResult(ok, "已登录" if ok else "会话无效，请重新扫码",
                           self._display_name())

    def _display_name(self) -> str:
        return str(self._profile.get("name") or self._profile.get("nickname") or "")

    def start_qr_login(self) -> QrLoginSession:
        """开始一次扫码登录（复用同一个 Cookie 会话）。"""
        session = QrLoginSession(self.session, self.host, timeout=self.timeout)
        session.start()
        return session

    def finish_qr_login(self, session: QrLoginSession, wx_code: str) -> LoginResult:
        """用轮询到的 wx_code 换 Session 并落盘。"""
        if not session.finish(wx_code):
            return LoginResult(False, "二维码确认失败，请重新扫码")
        self.save_cookies()
        self._login_ok = None
        self._login_checked_at = 0.0
        return self.ensure_login(force=True)

    def fetch_qr_image(self, session: QrLoginSession) -> bytes:
        """下载微信官方二维码图片（实测约 45 KB JPEG）。

        走微信域名，既不带 `xtbz` 头，也不参与本机限速。
        """
        challenge = session.challenge
        if challenge is None:
            raise YktError("尚未生成二维码")
        try:
            resp = self.session.get(challenge.qr_image_url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise YktError(f"二维码下载失败：{exc}") from exc
        if resp.status_code != 200 or not resp.content:
            raise YktError(f"二维码下载失败：HTTP {resp.status_code}")
        return resp.content

    # ------------------------------------------------------------ 读取

    def fetch_profile(self) -> dict[str, Any]:
        """返回当前登录者的资料（仅用于界面展示，**不落盘**）。"""
        if not self._profile:
            self.ensure_login()
        return dict(self._profile)

    def fetch_courses(self, latest_only: bool = True,
                      force: bool = False) -> list[Course]:
        """课程/教学班列表；`latest_only` 只保留最新学期。"""
        now = time.monotonic()
        if force or self._courses_cache is None or \
                (now - self._courses_at) > COURSES_TTL:
            payload = self._get_json(ep.courses_list(self.host))
            courses = rp.parse_courses(payload)
            if not courses:
                raise YktError("课程列表为空：请确认已登录且账号为学生身份")
            self._courses_cache = courses
            self._courses_at = now
        courses = self._courses_cache
        return rp.filter_courses(courses) if latest_only else list(courses)

    def fetch_activities(self, course: Course) -> list[Activity]:
        """一门课的全部教学活动（含截止时间与完成状态）。"""
        payload = self._get_json(ep.learn_logs(self.host, course.classroom_id))
        return rp.parse_activities(payload, course)

    def fetch_pending(self, courses: Iterable[Course],
                      now: datetime | None = None) -> list[Activity]:
        """把多门课的未完成作业汇总，按截止时间升序（无截止的排最后）。"""
        out: list[Activity] = []
        for course in courses:
            out.extend(rp.pending_homework(self.fetch_activities(course), now))
        # 跨课程统一排序：没有截止时间的排在最后
        out.sort(key=lambda a: (a.end_ms <= 0, a.end_ms))
        return out

    def fetch_notices(self, courses: Iterable[Course],
                      per_course: int = 10) -> list[Notice]:
        """各课公告汇总（按发布时间倒序，最新的在前）。"""
        out: list[Notice] = []
        for course in courses:
            payload = self._get_json(
                ep.announcements(self.host, course.classroom_id, limit=per_course))
            out.extend(rp.parse_announcements(payload, course))
        out.sort(key=lambda n: n.publish_ms, reverse=True)
        return out

    def fetch_system_messages(self) -> list[Notice]:
        """平台系统消息。"""
        payload = self._get_json(ep.system_message(self.host))
        return rp.parse_system_messages(payload)

    def fetch_upcoming_exams(self) -> list[Exam]:
        """课中即将开始的考试（学生端没有课后考试清单，见 DESIGN.md §1.8）。"""
        try:
            payload = self._get_json(ep.upcoming_exam(self.host))
        except NotLoggedIn:
            raise
        except YktError as exc:
            log.debug("临近考试接口不可用：%s", exc)
            return []
        return rp.parse_upcoming_exams(payload)
