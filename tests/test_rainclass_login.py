"""扫码登录状态机的离线单测（fake HTTP，零网络）。"""

from __future__ import annotations

import pytest

from xxt.rainclass.login import (AUTH_PARAM_PATH, LONG_POLL_URL,
                                 LoginError, PollResult, QrLoginSession,
                                 QrState, build_qrconnect_url, extract_uuid,
                                 parse_poll_response)

HOST = "https://www.yuketang.cn"
APP_ID = "wxTESTAPPID000000"
STATE = "eyJ0eXAiOiJKV1QifQ.test-state"
REDIRECT = "https://www.yuketang.cn/api/v3/user/login/wechat-web-callback"
UUID = "091ggmkY0pf4Ga1p"

#: 与真实 qrconnect 页面同构的最小 HTML
QR_HTML = (
    '<html><head><title>微信登录</title></head><body>'
    '<img class="qrcode" src="https://open.weixin.qq.com/connect/qrcode/'
    f'{UUID}"/>'
    '<script src="https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid='
    f'{UUID}"></script></body></html>'
)


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeHttp:
    """按 URL 前缀匹配的假 HTTP 客户端。"""

    def __init__(self, get_map=None, post_map=None, default=None):
        self.get_map = get_map or {}
        self.post_map = post_map or {}
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def _dispatch(self, table, url):
        self.calls.append(("GET" if table is self.get_map else "POST", url))
        for prefix, resp in table.items():
            if url.startswith(prefix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        if self.default is not None:
            return self.default
        raise AssertionError(f"unexpected request: {url}")

    def get(self, url, **kw):
        return self._dispatch(self.get_map, url)

    def post(self, url, **kw):
        return self._dispatch(self.post_map, url)


def auth_ok() -> FakeResponse:
    return FakeResponse(200, payload={"code": 0, "msg": "OK", "data": {
        "appId": APP_ID, "state": STATE, "redirectUri": REDIRECT}})


def make_http(poll_text: str = "window.wx_errcode=408;", *, auth=None,
              qr_text: str = QR_HTML, poll_status: int = 200) -> FakeHttp:
    return FakeHttp(
        get_map={
            "https://open.weixin.qq.com/connect/qrconnect": FakeResponse(
                200, text=qr_text),
            LONG_POLL_URL: FakeResponse(poll_status, text=poll_text),
            REDIRECT: FakeResponse(302, text=""),
        },
        post_map={f"{HOST}{AUTH_PARAM_PATH}": auth or auth_ok()},
    )


@pytest.fixture
def clock():
    box = [1000.0]
    return box


def session(http: FakeHttp, clock, **kw) -> QrLoginSession:
    return QrLoginSession(http, HOST, now=lambda: clock[0], **kw)


# ----------------------------------------------------------------- 纯函数


class TestHelpers:
    def test_extract_uuid(self):
        assert extract_uuid(QR_HTML) == UUID

    def test_extract_uuid_missing(self):
        with pytest.raises(LoginError):
            extract_uuid("<html>no qr here</html>")
        with pytest.raises(LoginError):
            extract_uuid("")

    def test_build_qrconnect_url_escapes_redirect(self):
        url = build_qrconnect_url(APP_ID, REDIRECT, STATE)
        assert url.startswith("https://open.weixin.qq.com/connect/qrconnect?")
        assert f"appid={APP_ID}" in url
        assert "redirect_uri=https%3A%2F%2Fwww.yuketang.cn%2Fapi%2Fv3" in url
        assert f"state={STATE}" in url
        assert "scope=snsapi_login" in url


class TestParsePollResponse:
    @pytest.mark.parametrize("errcode,expected", [
        (408, QrState.WAITING),
        (404, QrState.SCANNED),
        (403, QrState.EXPIRED),
    ])
    def test_simple_codes(self, errcode, expected):
        result = parse_poll_response(f"window.wx_errcode={errcode};")
        assert result.state is expected
        assert result.errcode == errcode

    def test_confirmed_with_wx_code(self):
        result = parse_poll_response(
            "window.wx_errcode=405;window.wx_code='021abcDEF';")
        assert result.state is QrState.CONFIRMED
        assert result.wx_code == "021abcDEF"

    def test_405_without_code_is_error(self):
        assert parse_poll_response("window.wx_errcode=405;").state is QrState.ERROR

    def test_empty_body_means_expired(self):
        assert parse_poll_response("").state is QrState.EXPIRED
        assert parse_poll_response("   ").state is QrState.EXPIRED

    def test_garbage_is_error(self):
        assert parse_poll_response("<html>502</html>").state is QrState.ERROR

    def test_unknown_code_is_error(self):
        result = parse_poll_response("window.wx_errcode=999;")
        assert result.state is QrState.ERROR
        assert result.errcode == 999


# ------------------------------------------------------------------- start


class TestStart:
    def test_happy_path(self, clock):
        http = make_http()
        ch = session(http, clock).start()

        assert ch.uuid == UUID
        assert ch.app_id == APP_ID
        assert ch.state == STATE
        assert ch.redirect_uri == REDIRECT
        assert ch.qr_image_url == \
            "https://open.weixin.qq.com/connect/qrcode/" + UUID
        assert ch.created_at == 1000.0

    def test_uses_post_for_auth_param(self, clock):
        # 实测 GET 返回 405，必须走 POST
        http = make_http()
        session(http, clock).start()
        assert http.calls[0][0] == "POST"
        assert http.calls[0][1].endswith(AUTH_PARAM_PATH)
        assert http.calls[1][0] == "GET"

    def test_auth_param_http_error(self, clock):
        http = make_http(auth=FakeResponse(405, text=""))
        with pytest.raises(LoginError, match="405"):
            session(http, clock).start()

    def test_auth_param_missing_state(self, clock):
        http = make_http(auth=FakeResponse(200, payload={"code": 0, "data": {
            "appId": APP_ID}}))
        with pytest.raises(LoginError, match="appId/state"):
            session(http, clock).start()

    def test_qr_page_without_uuid(self, clock):
        http = make_http(qr_text="<html>blocked</html>")
        with pytest.raises(LoginError, match="uuid"):
            session(http, clock).start()

    def test_network_exception_becomes_login_error(self, clock):
        http = make_http()
        http.post_map[f"{HOST}{AUTH_PARAM_PATH}"] = ConnectionError("boom")
        with pytest.raises(LoginError, match="boom"):
            session(http, clock).start()


# -------------------------------------------------------------------- poll


class TestPoll:
    def test_waiting_then_scanned_then_confirmed(self, clock):
        http = make_http()
        s = session(http, clock)
        s.start()

        assert s.poll().state is QrState.WAITING

        http.get_map[LONG_POLL_URL] = FakeResponse(
            200, text="window.wx_errcode=404;")
        assert s.poll().state is QrState.SCANNED

        http.get_map[LONG_POLL_URL] = FakeResponse(
            200, text="window.wx_errcode=405;window.wx_code='CODE1';")
        result = s.poll()
        assert result.state is QrState.CONFIRMED
        assert result.wx_code == "CODE1"

    def test_poll_includes_uuid_and_cache_buster(self, clock):
        http = make_http()
        s = session(http, clock)
        s.start()
        s.poll()
        poll_url = [u for m, u in http.calls if u.startswith(LONG_POLL_URL)][0]
        assert f"uuid={UUID}" in poll_url
        assert "_=1000000" in poll_url          # clock=1000.0 → 1000000 ms

    def test_expires_by_ttl_without_network(self, clock):
        http = make_http()
        s = session(http, clock, qr_ttl=300)
        s.start()
        clock[0] = 1300.0
        assert s.poll().state is QrState.EXPIRED
        # 已过期就不再发请求
        assert not [u for _, u in http.calls if u.startswith(LONG_POLL_URL)]

    def test_network_error_is_recoverable(self, clock):
        http = make_http()
        s = session(http, clock)
        s.start()
        http.get_map[LONG_POLL_URL] = ConnectionError("flaky")
        assert s.poll().state is QrState.ERROR

    def test_poll_before_start(self, clock):
        with pytest.raises(LoginError):
            session(make_http(), clock).poll()

    def test_http_error_status_is_error_state(self, clock):
        http = make_http(poll_status=502)
        s = session(http, clock)
        s.start()
        assert s.poll().state is QrState.ERROR


# ------------------------------------------------------------------ finish


class TestFinish:
    def test_redirect_success(self, clock):
        http = make_http()
        s = session(http, clock)
        s.start()
        assert s.finish("CODE1") is True
        url = [u for _, u in http.calls if u.startswith(REDIRECT)][0]
        assert "code=CODE1" in url
        assert STATE in url

    def test_json_failure_code(self, clock):
        http = make_http()
        http.get_map[REDIRECT] = FakeResponse(
            200, text='{"code":401,"msg":"state expired"}',
            payload={"code": 401, "msg": "state expired"})
        s = session(http, clock)
        s.start()
        assert s.finish("CODE1") is False

    def test_json_success_zero(self, clock):
        http = make_http()
        http.get_map[REDIRECT] = FakeResponse(
            200, text='{"code":0,"success":true}',
            payload={"code": 0, "success": True})
        s = session(http, clock)
        s.start()
        assert s.finish("CODE1") is True

    def test_http_error_status(self, clock):
        http = make_http()
        http.get_map[REDIRECT] = FakeResponse(500, text="")
        s = session(http, clock)
        s.start()
        assert s.finish("CODE1") is False

    def test_finish_before_start(self, clock):
        with pytest.raises(LoginError):
            session(make_http(), clock).finish("CODE1")


# ------------------------------------------------------------------- 杂项


class TestChallenge:
    def test_seconds_left_counts_down(self, clock):
        http = make_http()
        ch = session(http, clock, qr_ttl=300).start()
        # seconds_left 是给 UI 的便捷方法：不传 now 时按真实时间算，
        # 单测里显式传入受控时钟。
        assert ch.seconds_left(clock[0]) == 300
        clock[0] = 1180.0
        assert ch.seconds_left(clock[0]) == 120
        clock[0] = 9999.0
        assert ch.seconds_left(clock[0]) == 0

    def test_poll_result_defaults(self):
        result = PollResult(QrState.WAITING)
        assert result.wx_code == ""
        assert result.errcode is None
