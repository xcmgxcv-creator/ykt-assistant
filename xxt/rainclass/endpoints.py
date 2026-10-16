"""雨课堂 URL 构造。

所有路径拼装集中在这里：接缝只有「host 可配置」一条，
因此换学校（`changjiang.yuketang.cn` 等校级域名）只改配置，不动代码。
"""

from __future__ import annotations

import time
from urllib.parse import quote, urlencode, urlparse

DEFAULT_HOST = "https://www.yuketang.cn"

#: 客户端统一携带的自定义头。实测 `/v2/api/web/` 不需要它，
#: 但 `/mooc-api/`、`/c27/`、`/v/exam/` 缺了会被安全校验拦下，故一律带上。
REQUIRED_HEADERS = {"xtbz": "ykt"}

#: 网页端入口（登录失效时会 302 到这里）
PATH_INDEX = "/v2/web/index"


def normalize_host(host: str | None) -> str:
    """把配置里的 host 归一化成 `https://域名`（无尾斜杠）。"""
    h = (host or "").strip() or DEFAULT_HOST
    if not h.startswith(("http://", "https://")):
        h = "https://" + h
    return h.rstrip("/")


def host_of(url: str) -> str:
    """从 URL 里取回 `scheme://netloc`，用于跟随重定向后的校级域名。"""
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


# --------------------------------------------------------------------- 读取


def courses_list(host: str, identity: int = 2, ts_ms: int | None = None) -> str:
    """课程/教学班列表。`identity=2` 为学生身份。"""
    ts = ts_ms if ts_ms is not None else int(time.time() * 1000)
    q = urlencode({"identity": identity, "v": 0, "ts": ts})
    return f"{normalize_host(host)}/v2/api/web/courses/list?{q}"


def learn_logs(host: str, classroom_id: int, offset: int = 50,
               actype: int = -1, page: int = 0, sort: int = -1) -> str:
    """学习动态：待办明细的唯一来源（含截止时间与完成状态）。

    `offset` 实测是**每页条数**（传 5 返回 5 条，传 50 返回全部）。
    """
    q = urlencode({"actype": actype, "page": page, "offset": offset, "sort": sort})
    return f"{normalize_host(host)}/v2/api/web/logs/learn/{classroom_id}?{q}"


def announcements(host: str, classroom_id: int, limit: int = 10,
                  offset: int = 0, type_: int = 9) -> str:
    """课程公告。"""
    q = urlencode({"cid": classroom_id, "limit": limit, "offset": offset, "type": type_})
    return f"{normalize_host(host)}/v/discussion/v2/announcements/?{q}"


def system_message(host: str, client_type: int = 2) -> str:
    """平台系统消息（首页红点那条通道）。"""
    q = urlencode({"client_type": client_type})
    return f"{normalize_host(host)}/c27/online_courseware/ykt/system_message/?{q}"


def upcoming_exam(host: str) -> str:
    """课中即将开始的考试。学生端没有课后考试清单接口，见 DESIGN.md §1.8。"""
    return f"{normalize_host(host)}/api/v3/classroom/on-lesson-upcoming-exam"


# --------------------------------------------------------------------- 展示


def course_page(host: str, classroom_id: int, university_id: int = 0,
                platform_id: int = 3, tab: str = "unfinished") -> str:
    """课程「未完成」页 —— 用户在清单里点某条待办时打开的落地页。"""
    q = urlencode({
        "university_id": university_id,
        "platform_id": platform_id,
        "classroom_id": classroom_id,
        "tab": tab,
    })
    return f"{normalize_host(host)}/v2/web/studentLog/{classroom_id}?{q}"


def index_page(host: str) -> str:
    return f"{normalize_host(host)}{PATH_INDEX}"


# --------------------------------------------------------------------- 登录


def user_info_url(host: str) -> str:
    """取 csrftoken（写 cookie）。"""
    return f"{normalize_host(host)}/v/course_meta/user_info"


def wechat_auth_param_url(host: str) -> str:
    return f"{normalize_host(host)}/api/v3/user/login/wechat-auth-param"


def wechat_callback_url(host: str) -> str:
    return f"{normalize_host(host)}/api/v3/user/login/wechat-web-callback"


#: 微信开放平台的扫码页与长轮询端点（不在雨课堂域下，固定写死）
WECHAT_QRCONNECT = "https://open.weixin.qq.com/connect/qrconnect"
WECHAT_LQRCONNECT = "https://lp.open.weixin.qq.com/connect/l/qrconnect"


def wechat_qrconnect(appid: str, redirect_uri: str, state: str,
                     scope: str = "snsapi_login") -> str:
    """构造二维码内容（前端 `window.location` 跳的就是这个地址）。"""
    q = urlencode({
        "appid": appid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
    })
    return f"{WECHAT_QRCONNECT}?{q}#wechat_redirect"


def wechat_lqrconnect(appid: str, state: str, tip: int = 1) -> str:
    """长轮询用户是否已确认登录（返回 `window.location.origin + '/connect/l/qrconnect?uuid=…'` 形式）。"""
    q = urlencode({"uuid": state, "appid": appid, "_": int(time.time() * 1000), "tip": tip})
    return f"{WECHAT_LQRCONNECT}?{q}"


def callback_with_code(host: str, code: str, state: str) -> str:
    """扫码确认后带 wx_code 回调，换取 Session。"""
    q = urlencode({"code": code, "state": state})
    return f"{wechat_callback_url(host)}?{q}"


__all__ = [
    "DEFAULT_HOST",
    "REQUIRED_HEADERS",
    "normalize_host",
    "host_of",
    "courses_list",
    "learn_logs",
    "announcements",
    "system_message",
    "upcoming_exam",
    "course_page",
    "index_page",
    "user_info_url",
    "wechat_auth_param_url",
    "wechat_callback_url",
    "wechat_qrconnect",
    "wechat_lqrconnect",
    "callback_with_code",
    "quote",
]
