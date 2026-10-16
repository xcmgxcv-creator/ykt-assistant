"""雨课堂微信扫码登录状态机。

**纯逻辑**：不 import Qt，不直接依赖 requests——网络访问通过注入的 `http`
对象完成（只需 duck-typing：`.get(url, **kw)` / `.post(url, **kw)`，返回对象有
`status_code`、`text`、`json()`）。因此可用 fake 完整单测。

流程（2026-10-15 对真实环境只读探测所得）：

  ① POST  {host}/api/v3/user/login/wechat-auth-param
         → {"code":0,"data":{"appId":..., "state":<JWT>, "redirectUri":...}}
         （必须 POST：GET 返回 405）
  ② GET   https://open.weixin.qq.com/connect/qrconnect?appid=&redirect_uri=
          &response_type=code&scope=snsapi_login&state=
         → HTML 内含 uuid，且内嵌 <script src="lp.open.weixin.qq.com/...">
  ③ 二维码图片 = https://open.weixin.qq.com/connect/qrcode/{uuid}   （实测 JPEG）
  ④ GET   https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid=&_={ms}
         → window.wx_errcode=408/404/405/403; [window.wx_code='...;']
  ⑤ GET   {redirectUri}?code={wx_code}&state={state}   → 下发 Session Cookie

`state` 为短时效 JWT（实测 ext = iat + 3600s），`uuid` 约 5 分钟失效。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional
from urllib.parse import quote

from .endpoints import DEFAULT_HOST, normalize_host

#: 取授权参数的路径（注意：必须 POST）
AUTH_PARAM_PATH = "/api/v3/user/login/wechat-auth-param"
#: 微信扫码授权页
QRCONNECT_URL = "https://open.weixin.qq.com/connect/qrconnect"
#: 二维码图片前缀（拼 {uuid}）
QRCODE_IMAGE_URL = "https://open.weixin.qq.com/connect/qrcode/{uuid}"
#: 微信长轮询端点（网页版由 <script src> 驱动，我们直接 GET）
LONG_POLL_URL = "https://lp.open.weixin.qq.com/connect/l/qrconnect"

#: 二维码默认有效期（秒）
DEFAULT_QR_TTL = 300
#: 单次轮询的网络超时（秒）
DEFAULT_POLL_TIMEOUT = 15.0

#: 从 qrconnect 页面 HTML 中抠 uuid
_UUID_RE = re.compile(r"connect/qrcode/([A-Za-z0-9_\-]{8,})")
#: 微信轮询响应形如 window.wx_errcode=405;window.wx_code='xxx';
_ERRCODE_RE = re.compile(r"wx_errcode\s*=\s*(\d+)")
_WXCODE_RE = re.compile(r"wx_code\s*=\s*['\"]?([A-Za-z0-9_\-]+)")


class LoginError(RuntimeError):
    """登录流程中不可恢复的错误。"""


class QrState(str, Enum):
    """一次轮询的结果。"""

    WAITING = "waiting"      # 二维码已展示，尚未被扫描
    SCANNED = "scanned"      # 已扫码，等待用户在手机上确认
    CONFIRMED = "confirmed"  # 已确认，带上 wx_code 去换 Session
    EXPIRED = "expired"      # 二维码已被拒绝或超时，需要重新生成
    ERROR = "error"          # 响应无法解析 / 微信侧异常


@dataclass
class QrChallenge:
    """一次待扫描的二维码。"""

    uuid: str
    state: str
    app_id: str
    redirect_uri: str
    qr_image_url: str
    created_at: float = 0.0
    ttl: int = DEFAULT_QR_TTL

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.created_at) and (now - self.created_at) >= self.ttl

    def seconds_left(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        if not self.created_at:
            return self.ttl
        return max(0, int(self.ttl - (now - self.created_at)))


@dataclass
class PollResult:
    """轮询结果：状态 + （确认时）wx_code。"""

    state: QrState
    wx_code: str = ""
    raw: str = ""
    errcode: Optional[int] = None


def build_qrconnect_url(app_id: str, redirect_uri: str, state: str) -> str:
    """拼微信扫码授权页地址。"""
    return (
        f"{QRCONNECT_URL}?appid={quote(app_id, safe='')}"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
        f"&response_type=code&scope=snsapi_login"
        f"&state={quote(state, safe='')}"
    )


def extract_uuid(html: str) -> str:
    """从 qrconnect 页面 HTML 中提取 uuid；失败抛 LoginError。"""
    match = _UUID_RE.search(html or "")
    if not match:
        raise LoginError("未能从微信扫码页解析出 uuid（页面结构可能已变化）")
    return match.group(1)


def parse_poll_response(text: str) -> PollResult:
    """解析微信长轮询响应。

    微信返回的是 JS 片段而非 JSON，例如::

        window.wx_errcode=405;window.wx_code='021abcDEF';
    """
    body = text or ""
    code_match = _ERRCODE_RE.search(body)
    if not code_match:
        # 微信在二维码过期时会返回空响应
        if not body.strip():
            return PollResult(QrState.EXPIRED, raw=body)
        return PollResult(QrState.ERROR, raw=body)

    errcode = int(code_match.group(1))
    if errcode == 405:
        wx_match = _WXCODE_RE.search(body)
        if wx_match:
            return PollResult(QrState.CONFIRMED, wx_code=wx_match.group(1),
                              raw=body, errcode=errcode)
        return PollResult(QrState.ERROR, raw=body, errcode=errcode)
    if errcode == 404:
        return PollResult(QrState.SCANNED, raw=body, errcode=errcode)
    if errcode == 408:
        return PollResult(QrState.WAITING, raw=body, errcode=errcode)
    if errcode == 403:
        return PollResult(QrState.EXPIRED, raw=body, errcode=errcode)
    return PollResult(QrState.ERROR, raw=body, errcode=errcode)


class QrLoginSession:
    """一次扫码登录会话（状态机）。

    典型用法::

        session = QrLoginSession(http, base_url)
        challenge = session.start()           # 拿去 UI 显示图片
        while True:
            result = session.poll()
            if result.state is QrState.CONFIRMED:
                session.finish(result.wx_code)
                break
            if result.state in (QrState.EXPIRED, QrState.ERROR):
                challenge = session.start()   # 重新生成
            time.sleep(2)
    """

    def __init__(self, http: Any, base_url: str = DEFAULT_HOST, *,
                 timeout: float = DEFAULT_POLL_TIMEOUT,
                 qr_ttl: int = DEFAULT_QR_TTL,
                 now: Callable[[], float] = time.time) -> None:
        self.http = http
        self.base_url = normalize_host(base_url)
        self.timeout = timeout
        self.qr_ttl = qr_ttl
        self._now = now
        self.challenge: Optional[QrChallenge] = None

    # ------------------------------------------------------------ 步骤 ①②③

    def start(self) -> QrChallenge:
        """取授权参数 → 打开微信扫码页 → 抠 uuid。"""
        params = self._fetch_auth_param()
        app_id = str(params.get("appId") or "")
        state = str(params.get("state") or "")
        redirect_uri = str(params.get("redirectUri") or
                           f"{self.base_url}/api/v3/user/login/wechat-web-callback")
        if not app_id or not state:
            raise LoginError("wechat-auth-param 未返回 appId/state")

        url = build_qrconnect_url(app_id, redirect_uri, state)
        try:
            resp = self.http.get(url, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一转 LoginError
            raise LoginError(f"打开微信扫码页失败：{exc}") from exc
        if getattr(resp, "status_code", 0) != 200:
            raise LoginError(f"微信扫码页返回 HTTP {getattr(resp, 'status_code', '?')}")

        uuid = extract_uuid(resp.text)
        self.challenge = QrChallenge(
            uuid=uuid,
            state=state,
            app_id=app_id,
            redirect_uri=redirect_uri,
            qr_image_url=QRCODE_IMAGE_URL.format(uuid=uuid),
            created_at=self._now(),
            ttl=self.qr_ttl,
        )
        return self.challenge

    def _fetch_auth_param(self) -> dict:
        url = f"{self.base_url}{AUTH_PARAM_PATH}"
        try:
            # 必须 POST —— 实测 GET 返回 405
            resp = self.http.post(url, json={}, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise LoginError(f"请求微信授权参数失败：{exc}") from exc
        if getattr(resp, "status_code", 0) != 200:
            raise LoginError(f"wechat-auth-param 返回 HTTP "
                             f"{getattr(resp, 'status_code', '?')}")
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise LoginError(f"wechat-auth-param 响应不是 JSON：{exc}") from exc
        data = (payload or {}).get("data") or {}
        if not isinstance(data, dict):
            raise LoginError("wechat-auth-param 的 data 字段结构异常")
        return data

    # ------------------------------------------------------------- 步骤 ④

    def poll(self) -> PollResult:
        """轮询一次扫码状态（不阻塞、不重试）。"""
        if self.challenge is None:
            raise LoginError("尚未调用 start()，没有可轮询的二维码")
        if self.challenge.is_expired(self._now()):
            return PollResult(QrState.EXPIRED)

        url = (f"{LONG_POLL_URL}?uuid={quote(self.challenge.uuid, safe='')}"
               f"&_={int(self._now() * 1000)}")
        try:
            resp = self.http.get(url, timeout=self.timeout)
        except Exception:  # noqa: BLE001 - 网络抖动按"继续等待"处理，不中断扫码
            return PollResult(QrState.ERROR)
        if getattr(resp, "status_code", 0) != 200:
            return PollResult(QrState.ERROR)
        return parse_poll_response(resp.text)

    # ------------------------------------------------------------- 步骤 ⑤

    def finish(self, wx_code: str) -> bool:
        """用 wx_code 换 Session（成功与否取决于服务端是否下发 cookie）。"""
        if self.challenge is None:
            raise LoginError("尚未调用 start()")
        url = (f"{self.challenge.redirect_uri}"
               f"?code={quote(wx_code, safe='')}"
               f"&state={quote(self.challenge.state, safe='')}")
        try:
            resp = self.http.get(url, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise LoginError(f"换取 Session 失败：{exc}") from exc

        status = getattr(resp, "status_code", 0)
        if status >= 400:
            return False
        # 服务端可能返回 JSON：{"code":0,...} 或直接 302 回首页
        text = (getattr(resp, "text", "") or "").strip()
        if text.startswith("{"):
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                return True
            code = payload.get("code", payload.get("errcode", 0))
            if code not in (0, None):
                return False
            return bool(payload.get("success", True))
        return True
