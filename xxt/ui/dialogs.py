"""登录对话框与设置对话框。"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QImageReader, QPixmap
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QSpinBox, QTabWidget, QVBoxLayout,
                               QWidget)

from .. import config, startup
from ..rainclass.client import LoginResult
from ..rainclass.login import QrState
from . import style

log = logging.getLogger("xxt.ui")


#: 扫码状态轮询间隔（秒）：雨课堂前端自己也是 12s 轮询，登录环节可以更快
POLL_INTERVAL = 2.0


class LoginDialog(QDialog):
    """雨课堂微信扫码登录。

    二维码直接用微信官方图片接口返回的图（本地不解码、不重新生成），
    轮询跑在后台线程；登录动作由注入的 client 完成，因此 UI 可注入假 client 测试。
    """

    completed = Signal(object)   # LoginResult
    qr_ready = Signal(bytes)
    status = Signal(str)
    expired = Signal()

    def __init__(self, client, parent=None, directory_fn=None):
        super().__init__(parent)
        self.client = client
        self.directory_fn = directory_fn
        self._stop = threading.Event()
        self._running = False
        self._dir_applied = False
        self.completed.connect(self._finished)
        self.qr_ready.connect(self._show_qr)
        self.status.connect(self._set_status)
        self.expired.connect(self._on_expired)
        self.setWindowTitle("登录雨课堂")
        self.setModal(True)
        self.setFixedWidth(360)
        theme = config.load().get("widget", {}).get("theme", "light")
        self.setStyleSheet(style.stylesheet(theme))
        self.setPalette(style.build_palette(theme))

        root = QVBoxLayout(self)
        root.setSpacing(10)

        tip = QLabel("用微信「扫一扫」登录。会话 Cookie 仅经 Windows DPAPI "
                     "加密后保存在本机，不会上传到任何服务器；"
                     "全部采集都是只读请求，不提交作业、不代考试。")
        tip.setWordWrap(True)
        tip.setObjectName("Meta")
        root.addWidget(tip)

        self.qr_label = QLabel("正在获取二维码…")
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setMinimumHeight(190)
        self.qr_label.setWordWrap(True)
        root.addWidget(self.qr_label)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setObjectName("Meta")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.hint)

        self.data_directory = QLineEdit(str(config.DATA_DIR))
        self.data_directory.setReadOnly(True)
        if not config.load().get("storage", {}).get("selected", False):
            directory_row = QHBoxLayout()
            directory_row.addWidget(self.data_directory, 1)
            browse = QPushButton("选择…")
            browse.clicked.connect(self._choose_directory)
            directory_row.addWidget(browse)
            root.addWidget(QLabel("本地数据保存位置（会话、缓存和设置）"))
            root.addLayout(directory_row)

        buttons = QDialogButtonBox()
        self.refresh_btn = buttons.addButton("刷新二维码",
                                             QDialogButtonBox.ButtonRole.ActionRole)
        self.refresh_btn.clicked.connect(self.restart)
        self.cancel_btn = buttons.addButton("稍后", QDialogButtonBox.ButtonRole.RejectRole)
        self.cancel_btn.clicked.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------ 生命周期

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        super().showEvent(event)
        if not self._running:
            self.restart()

    def restart(self) -> None:
        """（重新）生成二维码；旧轮询线程会被 stop 事件唤醒后退出。"""
        if not self._apply_directory():
            return
        self._stop.set()
        stop = threading.Event()
        self._stop = stop
        self._running = True
        self.qr_label.setPixmap(QPixmap())
        self.qr_label.setText("正在获取二维码…")
        self.hint.setText("")
        threading.Thread(target=self._worker, args=(stop,),
                         name="ykt-login", daemon=True).start()

    def reject(self) -> None:
        self._stop.set()
        self._running = False
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        self._stop.set()
        self._running = False
        super().closeEvent(event)

    # ------------------------------------------------------------ 后台线程

    def _worker(self, stop: threading.Event) -> None:
        try:
            session = self.client.start_qr_login()
        except Exception as exc:  # noqa: BLE001 - 网络/结构异常都转成提示
            self.completed.emit(LoginResult(False, f"获取二维码失败：{exc}"))
            return
        try:
            self.qr_ready.emit(self.client.fetch_qr_image(session))
        except Exception:  # noqa: BLE001 - 图片拉不到不致命，仍可扫码
            pass
        self.status.emit("请用微信扫描二维码")

        while not stop.is_set():
            if stop.wait(POLL_INTERVAL):
                return
            try:
                result = session.poll()
            except Exception:  # noqa: BLE001 - 单次轮询异常按继续等待处理
                continue
            if result.state is QrState.SCANNED:
                self.status.emit("已扫码，请在手机上确认登录")
            elif result.state is QrState.CONFIRMED:
                self.status.emit("正在完成登录…")
                try:
                    res = self.client.finish_qr_login(session, result.wx_code)
                except Exception as exc:  # noqa: BLE001
                    res = LoginResult(False, f"登录失败：{exc}")
                self.completed.emit(res)
                return
            elif result.state is QrState.EXPIRED:
                self.expired.emit()
                return
            # WAITING / ERROR：继续轮询

    # ------------------------------------------------------------ 槽

    @Slot(bytes)
    def _show_qr(self, data: bytes) -> None:
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            self.qr_label.setPixmap(pixmap.scaled(
                180, 180, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        else:
            self.qr_label.setText("二维码图片加载失败，请点「刷新二维码」")

    @Slot(str)
    def _set_status(self, text: str) -> None:
        self.hint.setText(text)

    @Slot()
    def _on_expired(self) -> None:
        self._running = False
        self.qr_label.setText("二维码已过期")
        self.hint.setText("请点「刷新二维码」重新获取")

    @Slot(object)
    def _finished(self, res) -> None:
        self._running = False
        self._stop.set()
        if res.ok:
            self.accept()
        else:
            self.hint.setText(res.msg or "登录失败，请重试")

    # ------------------------------------------------------------ 数据目录

    def _apply_directory(self) -> bool:
        """首次启动轮询前切换数据目录（切换要求同步/数据库均已关闭）。"""
        if self._dir_applied or not self.directory_fn:
            return True
        try:
            self.directory_fn(self.data_directory.text())
        except Exception as exc:
            self.hint.setText(f"无法设置数据目录：{exc}")
            return False
        self._dir_applied = True
        return True

    def _choose_directory(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择雨课堂小助手的数据文件夹",
                                                self.data_directory.text())
        if path:
            self.data_directory.setText(path)
            self._dir_applied = False


class SettingsDialog(QDialog):
    """设置：同步 / 提醒 / 界面 / 维护 四个标签页。"""

    def __init__(self, parent=None, store=None):
        super().__init__(parent)
        self.store = store
        self.reset_requested = False
        self.logout_requested = False
        # 标题栏不写文字（四个标签页已经说明这是什么窗口）。
        # 注意：title 置空时 Windows 会退回显示可执行文件名（"python"），
        # 用一个空格才能真正空着。
        self.setWindowTitle(" ")
        self.setModal(True)
        self.setFixedWidth(430)
        cfg = config.load()
        theme = cfg.get("widget", {}).get("theme", "light")
        self.setStyleSheet(style.stylesheet(theme))
        self.setPalette(style.build_palette(theme))

        root = QVBoxLayout(self)
        root.setSpacing(10)

        tabs = QTabWidget()
        tabs.tabBar().setDrawBase(False)
        tabs.addTab(self._tab_sync(cfg), "同步")
        tabs.addTab(self._tab_remind(cfg), "提醒")
        tabs.addTab(self._tab_ui(cfg), "界面")
        tabs.addTab(self._tab_misc(), "维护")
        # 固定最小高度：切页时对话框不要跟着内容忽高忽低
        tabs.setMinimumHeight(360)
        self.tabs = tabs
        root.addWidget(tabs)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("保存")
        ok_btn.setObjectName("Primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ---------------- 同步

    def _tab_sync(self, cfg: dict) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(12, 14, 12, 12)
        form.setSpacing(10)
        s = cfg.get("sync", {})

        self.interval = QSpinBox()
        self.interval.setRange(5, 720)
        self.interval.setSuffix(" 分钟")
        self.interval.setValue(int(s.get("interval_minutes", 30)))
        form.addRow("自动同步间隔", self.interval)

        self.delay = QDoubleSpinBox()
        self.delay.setRange(0.2, 5.0)
        self.delay.setSingleStep(0.1)
        self.delay.setSuffix(" 秒/请求")
        self.delay.setValue(float(s.get("request_delay", 0.7)))
        form.addRow("请求限速", self.delay)

        self.detail_hours = QSpinBox()
        self.detail_hours.setRange(1, 72)
        self.detail_hours.setSuffix(" 小时")
        self.detail_hours.setValue(int(s.get("detail_refresh_hours", 6)))
        form.addRow("作业详情刷新", self.detail_hours)

        tip = QLabel("间隔越短越及时，但请求越频繁。建议不低于 15 分钟。")
        tip.setObjectName("Meta")
        tip.setWordWrap(True)
        form.addRow(tip)
        form.addRow(self._stretch())
        return page

    # ---------------- 提醒

    def _tab_remind(self, cfg: dict) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(12, 14, 12, 12)
        form.setSpacing(10)
        r = cfg.get("remind", {})

        self.remind_enabled = QCheckBox("启用桌面提醒（右下角弹通知）")
        self.remind_enabled.setChecked(bool(r.get("enabled", True)))
        form.addRow(self.remind_enabled)

        self.thresholds = QLineEdit(",".join(str(x) for x in
                                             r.get("threshold_hours", [24, 6, 1])))
        self.thresholds.setPlaceholderText("例如 24,6,1")
        form.addRow("截止提醒档位(小时)", self.thresholds)

        self.exam_leads = QLineEdit(",".join(str(x) for x in
                                             r.get("exam_lead_hours", [24, 1])))
        self.exam_leads.setPlaceholderText("例如 24,1")
        form.addRow("考试提醒档位(小时)", self.exam_leads)

        quiet = QHBoxLayout()
        q0, q1 = (r.get("quiet_hours") or [23, 7])[:2]
        self.quiet_start = QSpinBox()
        self.quiet_start.setRange(0, 23)
        self.quiet_start.setValue(int(q0))
        self.quiet_end = QSpinBox()
        self.quiet_end.setRange(0, 23)
        self.quiet_end.setValue(int(q1))
        quiet.addWidget(self.quiet_start)
        quiet.addWidget(QLabel("点 至"))
        quiet.addWidget(self.quiet_end)
        quiet.addWidget(QLabel("点"))
        quiet.addStretch(1)
        form.addRow("免打扰时段", quiet)

        self.new_homework = QCheckBox("发现新作业时提醒")
        self.new_homework.setChecked(bool(r.get("new_homework", True)))
        self.new_exam = QCheckBox("发现新考试时提醒")
        self.new_exam.setChecked(bool(r.get("new_exam", True)))
        self.new_notice = QCheckBox("发现新通知时提醒（通知较多，默认关）")
        self.new_notice.setChecked(bool(r.get("new_notice", False)))
        form.addRow(self.new_homework)
        form.addRow(self.new_exam)
        form.addRow(self.new_notice)
        form.addRow(self._stretch())
        return page

    # ---------------- 界面

    def _tab_ui(self, cfg: dict) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(12, 14, 12, 12)
        form.setSpacing(10)
        w = cfg.get("widget", {})

        self.always_on_top = QCheckBox("窗口始终置顶")
        self.always_on_top.setChecked(bool(w.get("always_on_top", False)))
        self.dark = QCheckBox("深色主题")
        self.dark.setChecked(w.get("theme") == "dark")

        self.opacity = QSpinBox()
        self.opacity.setRange(60, 100)
        self.opacity.setSuffix(" %")
        self.opacity.setValue(int(round(float(w.get("opacity", 0.97)) * 100)))

        self.show_notices = QCheckBox("在列表底部显示未读通知")
        self.show_notices.setChecked(bool(w.get("show_notices", True)))
        self.autostart = QCheckBox("开机自动启动")
        self.autostart.setChecked(startup.is_enabled())

        for row in (self.always_on_top, self.dark):
            form.addRow(row)
        form.addRow("不透明度", self.opacity)
        for row in (self.show_notices, self.autostart):
            form.addRow(row)
        self.background_path = QLineEdit(w.get("background_image", ""))
        self.background_path.setReadOnly(True)
        self.background_path.setPlaceholderText("使用默认纯色背景")
        background_row = QHBoxLayout()
        background_row.addWidget(self.background_path, 1)
        choose = QPushButton("选择")
        choose.clicked.connect(self._choose_background)
        clear = QPushButton("移除")
        clear.clicked.connect(self.background_path.clear)
        background_row.addWidget(choose)
        background_row.addWidget(clear)
        form.addRow("背景图", background_row)
        self.background_overlay = QSpinBox()
        self.background_overlay.setRange(20, 95)
        self.background_overlay.setSuffix(" %")
        self.background_overlay.setValue(int(w.get("background_overlay", 78)))
        form.addRow("文字保护蒙层", self.background_overlay)
        self.background_preview = QLabel()
        self.background_preview.setFixedHeight(65)
        self.background_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form.addRow(self.background_preview)
        self._update_background_preview()
        self.background_path.textChanged.connect(self._update_background_preview)
        form.addRow(self._stretch())
        return page

    # ---------------- 维护

    def _tab_misc(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(12, 14, 12, 12)
        box.setSpacing(10)

        acc = QGroupBox("登录状态")
        acc_lay = QVBoxLayout(acc)
        acc_lay.setSpacing(8)
        self.account_label = QLabel("已登录（本机会话已用 DPAPI 加密保存）"
                                    if config.COOKIE_PATH.exists() else "尚未登录")
        self.account_label.setObjectName("Meta")
        acc_lay.addWidget(self.account_label)
        clear_btn = QPushButton("退出登录（清除本机会话）")
        clear_btn.clicked.connect(self._clear_credentials)
        acc_lay.addWidget(clear_btn)
        box.addWidget(acc)

        item_box = QGroupBox("任务清单")
        item_lay = QVBoxLayout(item_box)
        item_lay.setSpacing(8)
        self.hidden_label = QLabel("")
        self.hidden_label.setObjectName("Meta")
        item_lay.addWidget(self.hidden_label)
        self.restore_btn = QPushButton("恢复被移除的条目")
        self.restore_btn.clicked.connect(self._restore_hidden)
        item_lay.addWidget(self.restore_btn)
        open_btn = QPushButton("打开数据目录")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(config.DATA_DIR))))
        item_lay.addWidget(open_btn)
        box.addWidget(item_box)
        self.data_directory = QLineEdit(str(config.DATA_DIR))
        self.data_directory.setReadOnly(True)
        self.data_directory.setToolTip(str(config.DATA_DIR))
        location_row = QHBoxLayout()
        location_row.addWidget(self.data_directory, 1)
        choose_location = QPushButton("更改位置…")
        choose_location.clicked.connect(self._choose_data_directory)
        location_row.addWidget(choose_location)
        box.addLayout(location_row)
        clear_all = QPushButton("清除全部本地数据…")
        clear_all.setObjectName("Danger")
        clear_all.clicked.connect(self._clear_all)
        box.addWidget(clear_all)
        self._refresh_hidden_label()
        box.addStretch(1)
        return page

    def _stretch(self) -> QWidget:
        """占位：让表单内容顶部对齐，不要被纵向拉伸。"""
        holder = QWidget()
        holder.setFixedHeight(1)
        return holder

    # ---------------- 动作

    def _choose_background(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择背景图", "",
                                              "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
        if path:
            reader = QImageReader(path)
            if not reader.canRead():
                QMessageBox.warning(self, "图片无法读取", "请选择有效的图片文件。")
                return
            self.background_path.setText(path)

    def _update_background_preview(self) -> None:
        pm = QPixmap(self.background_path.text()) if self.background_path.text() else QPixmap()
        if pm.isNull():
            self.background_preview.clear()
        else:
            self.background_preview.setPixmap(pm.scaled(240, 65,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _choose_data_directory(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择新的数据文件夹",
                                                self.data_directory.text())
        if path:
            self.data_directory.setText(path)

    def _clear_all(self) -> None:
        if QMessageBox.question(self, "清除全部本地数据",
                "将退出登录并清除本机保存的登录会话、作业、考试、公告、背景图和设置。\n"
                "雨课堂上的数据不受影响，所选文件夹内的其他文件会保留。\n是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.reset_requested = True
            self.reject()

    def _refresh_hidden_label(self) -> None:
        count = self.store.hidden_count() if self.store else 0
        if count:
            self.hidden_label.setText(f"已从列表移除 {count} 项（不影响雨课堂上的数据）")
        else:
            self.hidden_label.setText("没有已移除的条目")
        self.restore_btn.setEnabled(bool(count))

    def _restore_hidden(self) -> None:
        if not self.store:
            return
        n = self.store.restore_hidden()
        self._refresh_hidden_label()
        QMessageBox.information(self, "已恢复", f"恢复了 {n} 项，关闭设置后即可看到。")

    def _clear_credentials(self) -> None:
        if QMessageBox.question(self, "确认", "确定要退出登录并清除本机会话吗？") \
                == QMessageBox.StandardButton.Yes:
            config.clear_credentials()
            config.COOKIE_PATH.unlink(missing_ok=True)
            self.logout_requested = True
            self.account_label.setText("尚未登录")

    @staticmethod
    def _parse_hours(text: str, fallback: list[int]) -> list[int]:
        out = []
        for part in (text or "").replace("，", ",").split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
        return sorted(set(out), reverse=True) or fallback

    def save(self) -> None:
        background = self.background_path.text()
        if background:
            reader = QImageReader(background)
            reader.setAutoTransform(True)
            size = reader.size()
            if max(size.width(), size.height()) > 4096:
                size.scale(4096, 4096, Qt.AspectRatioMode.KeepAspectRatio)
                reader.setScaledSize(size)
            image = reader.read()
            # Windows 上读取托管背景后要先释放文件句柄，才能原子替换同一路径。
            del reader
            if image.isNull():
                raise OSError("背景图片读取失败，请重新选择")
            config.ensure_dirs()
            target = config.DATA_DIR / "background.png"
            temporary = target.with_suffix(".tmp")
            if not image.save(str(temporary), "PNG"):
                raise OSError("背景图片保存失败")
            temporary.replace(target)
            background = str(target)
        else:
            (config.DATA_DIR / "background.png").unlink(missing_ok=True)
        config.update("sync",
                      interval_minutes=self.interval.value(),
                      request_delay=round(self.delay.value(), 2),
                      detail_refresh_hours=self.detail_hours.value())
        config.update("remind",
                      enabled=self.remind_enabled.isChecked(),
                      threshold_hours=self._parse_hours(self.thresholds.text(),
                                                        [24, 6, 1]),
                      exam_lead_hours=self._parse_hours(self.exam_leads.text(),
                                                        [24, 1]),
                      quiet_hours=[self.quiet_start.value(), self.quiet_end.value()],
                      new_homework=self.new_homework.isChecked(),
                      new_exam=self.new_exam.isChecked(),
                      new_notice=self.new_notice.isChecked())
        config.update("widget",
                      always_on_top=self.always_on_top.isChecked(),
                      theme="dark" if self.dark.isChecked() else "light",
                      opacity=round(self.opacity.value() / 100.0, 2),
                      show_notices=self.show_notices.isChecked(),
                      background_image=background,
                      background_overlay=self.background_overlay.value())
        if self.autostart.isChecked() != startup.is_enabled():
            if not startup.set_enabled(self.autostart.isChecked()):
                QMessageBox.warning(self, "提示", "开机自启设置失败，可稍后重试。")
