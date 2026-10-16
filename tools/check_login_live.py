"""真机自检：验证「能真的拿到微信二维码并显示」——不需要人扫码。

    .venv\\Scripts\\python.exe tools\\check_login_live.py

走的是真实网络：POST `wechat-auth-param` → 拉 `open.weixin.qq.com/connect/qrconnect`
→ 正则抠 uuid → 下载官方二维码图片 → 显示在 `LoginDialog` 里。

成功判据：二维码图片下载并上屏（exit 0）。截图写到 data/login_dialog.png，
方便肉眼确认排版。整个过程只做只读请求，不会真的登录，也不会落盘任何凭据。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer                                  # noqa: E402
from PySide6.QtWidgets import QApplication                         # noqa: E402

from xxt import config                                             # noqa: E402
from xxt.rainclass.client import RainClassClient                   # noqa: E402
from xxt.ui.dialogs import LoginDialog                             # noqa: E402

result: dict[str, object] = {"shown": False, "hint": ""}


def main() -> int:
    config.ensure_dirs()
    app = QApplication(sys.argv[:1])

    client = RainClassClient(request_delay=0.0)
    print(f"平台域名：{client.host}")
    dialog = LoginDialog(client)

    def capture() -> None:
        pixmap = dialog.qr_label.pixmap()
        result["shown"] = bool(pixmap) and not pixmap.isNull()
        result["hint"] = dialog.hint.text()
        shot = config.DATA_DIR / "login_dialog.png"
        dialog.grab().save(str(shot))
        print(f"二维码已上屏：{result['shown']}")
        print(f"提示文字：{result['hint']}")
        print(f"截图：{shot}")
        if result["shown"]:
            print(f"二维码尺寸：{pixmap.width()}x{pixmap.height()}")
        app.quit()

    dialog.show()
    QTimer.singleShot(15000, capture)
    app.exec()
    return 0 if result["shown"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
