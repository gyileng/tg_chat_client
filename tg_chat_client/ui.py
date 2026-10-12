import re
import time
from datetime import datetime, timedelta
from math import ceil
from uuid import uuid4

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QInputMethodEvent,
    QKeyEvent,
    QPainter,
    QPixmap,
    QPolygon,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .message_cache import MessageCache
from .message_view import MESSAGE_ROLE, MessageList, media_text
from .models import BEIJING, ProxyConfig, SendRequest, day_key
from .native_ime import cancel_windows_composition
from .store import validate_phrase_alias


class Composer(QTextEdit):
    send_requested = Signal()

    def __init__(self):
        super().__init__()
        self.composing = False
        self.resetting_input_method = False
        self.ime_reset_timer = QTimer(self)
        self.ime_reset_timer.setSingleShot(True)
        self.ime_reset_timer.timeout.connect(self.finish_ime_reset)
        self.ime_restore_timer = QTimer(self)
        self.ime_restore_timer.setSingleShot(True)
        self.ime_restore_timer.timeout.connect(self.restore_ime_focus)
        self.phrase_prefix = ";"
        self.phrase_aliases = {}
        self.setPlaceholderText("输入消息；Enter 发送，Shift+Enter 换行")
        self.setMaximumHeight(110)

    def inputMethodEvent(self, event):
        if self.resetting_input_method:
            event.accept()
            return
        selected = self.textCursor().hasSelection()
        expand = (
            not event.preeditString()
            and bool(re.search(r"[A-Za-z0-9_]$", event.commitString()))
            and not selected
        )
        self.composing = bool(event.preeditString())
        super().inputMethodEvent(event)
        preedit = event.preeditString()
        if not selected and re.fullmatch(r"[A-Za-z0-9_]+(?:[ '][A-Za-z0-9_]+)*", preedit):
            # Some Pinyin input methods separate syllables with spaces or apostrophes.
            self.expand_phrase(preedit.replace(" ", "").replace("'", ""))
        elif expand:
            self.expand_phrase()

    def expand_phrase(self, preedit=""):
        cursor = self.textCursor()
        if cursor.hasSelection():
            return False
        before = QTextCursor(cursor)
        before.setPosition(0, QTextCursor.MoveMode.KeepAnchor)
        prefix_pattern = "".join(
            "[;；]" if char in ";；" else re.escape(char) for char in self.phrase_prefix
        )
        match = re.search(prefix_pattern + r"([A-Za-z0-9_]+)$", before.selectedText() + preedit)
        if not match:
            return False
        text = self.phrase_aliases.get(match[1].lower())
        if text is None:
            return False
        after = QTextCursor(cursor)
        after.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
        if re.match(r"[A-Za-z0-9_]", after.selectedText()):
            return False
        if preedit:
            # Wait until the platform finishes delivering this composition event before
            # cancelling it. Resetting inside that event can leave native candidates alive.
            self.resetting_input_method = True
            commit = QInputMethodEvent("", [])
            commit.setCommitString(preedit)
            super().inputMethodEvent(commit)
            self.composing = False
            cursor = self.textCursor()
            self.ime_reset_timer.start(0)
        width = len(match[0].encode("utf-16-le")) // 2
        cursor.beginEditBlock()
        cursor.setPosition(cursor.position() - width, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(text)
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        return True

    def finish_ime_reset(self):
        try:
            if self.hasFocus():
                QApplication.inputMethod().reset()
                cancel_windows_composition(int(self.window().winId()))
                # TSF input methods can retain candidates after an IMM cancellation.
                # Remove the editable focus for one event-loop turn, then restore it.
                self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
                self.clearFocus()
                self.ime_restore_timer.start(0)
            super().inputMethodEvent(QInputMethodEvent("", []))
        finally:
            if not self.ime_restore_timer.isActive():
                self.resetting_input_method = False
            self.composing = False

    def restore_ime_focus(self):
        try:
            self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
            if (
                QApplication.focusWidget() is None
                and self.window().isActiveWindow()
                and self.isEnabled()
            ):
                self.setFocus(Qt.FocusReason.OtherFocusReason)
            super().inputMethodEvent(QInputMethodEvent("", []))
        finally:
            self.resetting_input_method = False
            self.composing = False

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.composing:
                super().keyPressEvent(event)
                return
            if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                if not event.isAutoRepeat():
                    self.send_requested.emit()
                event.accept()
                return
        expand = (
            not self.composing
            and not event.isAutoRepeat()
            and not self.textCursor().hasSelection()
            and not event.modifiers()
            & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.MetaModifier
            )
            and bool(re.fullmatch(r"[A-Za-z0-9_]", event.text()))
        )
        super().keyPressEvent(event)
        if expand:
            self.expand_phrase()


class SettingsDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("连接设置（所有账号共用）")
        self.setMinimumWidth(440)
        settings = store.export_settings()
        proxy = settings.get("proxy", {})
        layout = QFormLayout(self)
        self.api_id = QLineEdit(str(settings.get("api_id", "")))
        self.api_hash = QLineEdit(settings.get("api_hash", ""))
        self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.protocol = QComboBox()
        self.protocol.addItems(["socks5", "http"])
        self.protocol.setCurrentText(proxy.get("protocol", "socks5"))
        self.host = QLineEdit(proxy.get("host", "127.0.0.1"))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(proxy.get("port", 1080))
        self.username = QLineEdit(proxy.get("username", ""))
        self.password = QLineEdit(proxy.get("password", ""))
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        for title, widget in [
            ("API ID", self.api_id),
            ("API Hash", self.api_hash),
            ("代理协议", self.protocol),
            ("代理地址", self.host),
            ("代理端口", self.port),
            ("代理用户名（可选）", self.username),
            ("代理密码（可选）", self.password),
        ]:
            layout.addRow(title, widget)
        note = QLabel(
            "API 参数从 my.telegram.org 的 API development tools 获取。\n"
            "配置仅存于本机私有数据目录；保存后会重新连接全部账号。"
        )
        note.setWordWrap(True)
        layout.addRow(note)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addRow(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def save(self):
        try:
            proxy = ProxyConfig(
                self.protocol.currentText(),
                self.host.text().strip(),
                self.port.value(),
                self.username.text(),
                self.password.text(),
            )
            self.store.save_connection(int(self.api_id.text()), self.api_hash.text().strip(), proxy)
        except ValueError:
            self.error.setText("请检查 API ID、API Hash 和代理配置（地址只填写主机名或 IP）。")
            return
        self.accept()


class PhraseDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("管理共用短句（拖动排序）")
        self.resize(440, 420)
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        layout.addWidget(QLabel("共用前缀（一个或多个非空白符号）"))
        self.prefix = QLineEdit(store.phrase_prefix())
        layout.addWidget(self.prefix)
        note = QLabel("输入完整触发串立即替换，无需空格；缩写不能重复或互为前缀。")
        note.setWordWrap(True)
        layout.addWidget(note)
        for phrase in store.phrases():
            self.add_item(phrase)
        layout.addWidget(self.list)
        actions = QHBoxLayout()
        for title, callback in [("添加", self.add), ("编辑", self.edit), ("删除", self.delete)]:
            button = QPushButton(title)
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.list.itemDoubleClicked.connect(lambda _: self.edit())
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def add_item(self, phrase):
        item = QListWidgetItem()
        self.update_item(item, phrase)
        self.list.addItem(item)

    def update_item(self, item, phrase):
        item.setData(Qt.ItemDataRole.UserRole, phrase)
        item.setText((phrase.get("alias") or "未设缩写") + " · " + phrase["text"])

    def ask(self, phrase=None, exclude=None):
        phrase = phrase or {"text": "", "alias": ""}
        dialog = QDialog(self)
        dialog.setWindowTitle("短句")
        layout = QFormLayout(dialog)
        alias = QLineEdit(phrase.get("alias") or "")
        text = QTextEdit()
        text.setPlainText(phrase["text"])
        layout.addRow("缩写（可空，字母/数字/下划线）", alias)
        layout.addRow("正文（最多 4096 字符）", text)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        error = QLabel()
        error.setWordWrap(True)
        layout.addRow(error)

        def accept():
            try:
                validate_phrase_alias(
                    alias.text().strip().lower(),
                    [
                        (self.list.item(i).data(Qt.ItemDataRole.UserRole).get("alias") or "")
                        .strip()
                        .lower()
                        for i in range(self.list.count())
                        if self.list.item(i) is not exclude
                    ],
                )
                if not text.toPlainText().strip() or len(text.toPlainText().strip()) > 4096:
                    raise ValueError("正文须为 1–4096 个字符")
            except ValueError as exc:
                error.setText(str(exc))
                return
            dialog.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted and text.toPlainText().strip():
            return {"text": text.toPlainText().strip(), "alias": alias.text().strip().lower()}
        return None

    def add(self):
        value = self.ask()
        if value:
            self.add_item(value)

    def edit(self):
        item = self.list.currentItem()
        if item:
            value = self.ask(item.data(Qt.ItemDataRole.UserRole), exclude=item)
            if value:
                self.update_item(item, value)

    def delete(self):
        self.list.takeItem(self.list.currentRow())

    def save(self):
        try:
            self.store.replace_phrases(
                [
                    self.list.item(i).data(Qt.ItemDataRole.UserRole)
                    for i in range(self.list.count())
                ],
                prefix=self.prefix.text(),
            )
        except ValueError as exc:
            QMessageBox.information(self, "无法保存短句", str(exc))
            return
        self.accept()


class LoginDialog(QDialog):
    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.account_id = None
        self.pending = None
        self.password_needed = False
        self.setWindowTitle("添加账号 / 重新登录")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(420)
        layout = QFormLayout(self)
        self.phone = QLineEdit()
        self.phone.setPlaceholderText("+国家代码号码")
        self.code = QLineEdit()
        self.code.setEchoMode(QLineEdit.EchoMode.Password)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow("手机号", self.phone)
        layout.addRow("验证码", self.code)
        layout.addRow("两步验证密码", self.password)
        self.request_button = QPushButton("获取验证码")
        self.request_button.clicked.connect(self.request_code)
        self.login_button = QPushButton("登录")
        self.login_button.setEnabled(False)
        self.login_button.clicked.connect(self.login)
        layout.addRow(self.request_button)
        layout.addRow(self.login_button)
        self.notice = QLabel("验证码可能发送到你的 Telegram 官方客户端。")
        self.notice.setWordWrap(True)
        layout.addRow(self.notice)

    def request_code(self):
        if self.pending:
            return
        self.pending = self.bridge.submit("request_code", phone=self.phone.text())
        self.request_button.setEnabled(False)
        self.login_button.setEnabled(False)
        self.notice.setText("正在请求验证码…")

    def login(self):
        if self.pending or not self.account_id:
            return
        self.pending = self.bridge.submit(
            "sign_in",
            account_id=self.account_id,
            code=self.code.text().strip(),
            password=self.password.text() if self.password_needed else "",
        )
        self.login_button.setEnabled(False)
        self.notice.setText("正在登录…")

    def handle(self, event):
        if event.get("request_id") != self.pending:
            return False
        self.pending = None
        self.request_button.setEnabled(True)
        if event["kind"] == "error":
            self.notice.setText(event["message"])
            self.login_button.setEnabled(bool(self.account_id))
        elif event["operation"] == "request_code":
            self.account_id = event["result"]["account_id"]
            self.password_needed = False
            self.login_button.setEnabled(True)
            self.notice.setText("验证码已请求，请查看 Telegram 或短信。")
            self.code.setFocus()
        elif event["operation"] == "sign_in":
            self.password_needed = event["result"].get("password_needed", False)
            if self.password_needed:
                self.login_button.setEnabled(True)
                self.notice.setText("此账号需要两步验证密码。")
                self.password.setFocus()
            else:
                self.code.clear()
                self.password.clear()
                self.account_id = None
                self.accept()
        return True

    def reject(self):
        if self.pending:
            self.notice.setText("请等待当前登录请求完成后关闭。")
            return
        if self.account_id:
            self.bridge.submit("cancel_login", account_id=self.account_id)
        self.code.clear()
        self.password.clear()
        super().reject()

    def closeEvent(self, event):
        if self.pending:
            event.ignore()
        else:
            self.reject()
            event.accept()


class MainWindow(QMainWindow):
    quit_requested = Signal()

    def __init__(self, store, bridge, mock=False):
        super().__init__()
        self.store, self.bridge, self.mock = store, bridge, mock
        self.closing = False
        self.tray = None
        self.hidden_dialogs = []
        self.account_id = None
        self.chat_id = None
        self.groups_cache = {}
        self.cache = MessageCache()
        self.buffers = self.cache.recent
        self.accounts_dirty = True
        self.phrases_dirty = True
        self.counts_dirty = True
        self.ui_dirty = True
        self.account_records = {a["id"]: a for a in store.accounts()}
        self.counts_snapshot = {}
        self.count_day = day_key()
        self.resume_bottom = set()
        self.draft_dirty = set()
        self.notice_text = None
        self.statuses = {}
        self.pending_sends = {}
        self.blocked = {}
        self.blocked_accounts = {}
        self.group_requests = {}
        self.history_requests = {}
        self.history_before = {}
        self.login_dialog = None
        self.settings_dialog = None
        self.reply_id = None
        self.reply_text = ""
        self.loading_draft = False
        self.rendering = False
        self.resize_pending = False
        self.resize_anchor = None
        self.resize_rendering_state = False
        self.scroll_positions = {}
        self.history_exhausted = set()
        self.setWindowTitle("TG Chat Client" + (" — 模拟模式（不会连接 Telegram）" if mock else ""))
        self.resize(1160, 780)
        self.build_ui()
        self.setup_tray()
        self.bridge.event.connect(self.handle)
        self.refresh_accounts()
        self.refresh_phrases()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_counts)
        self.day_timer = QTimer(self)
        self.day_timer.setSingleShot(True)
        self.day_timer.timeout.connect(self.day_changed)
        self.schedule_midnight()
        self.shortcut_filter = ShortcutFilter(self)
        QApplication.instance().installEventFilter(self.shortcut_filter)
        QTimer.singleShot(0, self.start_service)
        if not mock and not store.export_settings():
            QTimer.singleShot(100, self.open_connection_settings)

    @property
    def target(self):
        return self.account_id, self.chat_id

    def start_service(self):
        if not self.closing:
            self.bridge.submit("start")

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        toolbar = QHBoxLayout()
        self.settings_button = QPushButton("连接设置")
        self.settings_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.settings_button.clicked.connect(lambda: self.open_connection_settings())
        self.settings_button.setObjectName("connectionSettingsButton")
        toolbar.addWidget(self.settings_button)
        for text, callback in [
            ("添加 / 重新登录", self.open_login),
            ("重新连接", self.reconnect),
            ("管理短句", self.manage_phrases),
        ]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        toolbar.addStretch()
        outer.addLayout(toolbar)
        settings_menu = self.menuBar().addMenu("设置")
        settings_action = QAction("连接设置", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(lambda: self.open_connection_settings())
        settings_menu.addAction(settings_action)
        application_menu = self.menuBar().addMenu("程序")
        self.exit_action = application_menu.addAction("退出")
        self.exit_action.triggered.connect(self.request_exit)
        self.notice = QLabel(
            "模拟数据与真实账号数据完全分开。" if self.mock else "请配置代理并添加账号。"
        )
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self.notice)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("账号 · 今日工具发送成功条数"))
        self.accounts = QListWidget()
        self.accounts.setMaximumHeight(250)
        self.accounts.currentItemChanged.connect(self.account_changed)
        self.account_actions = set()
        self.accounts.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.accounts.customContextMenuRequested.connect(self.account_menu)
        left_layout.addWidget(self.accounts)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索已加入的群")
        self.search.textChanged.connect(self.render_groups)
        left_layout.addWidget(self.search)
        self.groups = QListWidget()
        self.groups.currentItemChanged.connect(self.group_changed)
        self.groups.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.groups.customContextMenuRequested.connect(self.group_menu)
        left_layout.addWidget(self.groups)
        refresh = QPushButton("刷新群列表")
        refresh.clicked.connect(self.load_groups)
        left_layout.addWidget(refresh)
        splitter.addWidget(left)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.identity = QLabel("尚未选择账号和群")
        self.identity.setTextFormat(Qt.TextFormat.PlainText)
        right_layout.addWidget(self.identity)
        history_bar = QHBoxLayout()
        self.older = QPushButton("加载更早消息")
        self.older.clicked.connect(lambda: self.load_history(older=True))
        history_bar.addWidget(self.older)
        self.bottom = QPushButton("回到底部")
        self.bottom.clicked.connect(lambda: self.go_bottom())
        history_bar.addWidget(self.bottom)
        right_layout.addLayout(history_bar)
        self.messages = MessageList()
        self.message_resize_timer = QTimer(self)
        self.message_resize_timer.setSingleShot(True)
        self.message_resize_timer.timeout.connect(self.resize_messages)
        self.message_sync_timer = QTimer(self)
        self.message_sync_timer.setSingleShot(True)
        self.message_sync_timer.timeout.connect(self.render_messages)
        self.messages.viewport_resizing.connect(self.prepare_message_resize)
        self.messages.viewport_resized.connect(self.schedule_message_resize)
        self.messages.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.messages.doubleClicked.connect(self.quote)
        self.messages.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.messages.customContextMenuRequested.connect(self.message_menu)
        self.messages.verticalScrollBar().valueChanged.connect(self.scrolled)
        right_layout.addWidget(self.messages, 1)
        quote_bar = QHBoxLayout()
        self.quote_label = QLabel("双击消息或右键选择引用回复")
        self.quote_label.setTextFormat(Qt.TextFormat.PlainText)
        quote_bar.addWidget(self.quote_label, 1)
        cancel = QPushButton("取消引用")
        cancel.clicked.connect(self.cancel_quote)
        quote_bar.addWidget(cancel)
        right_layout.addLayout(quote_bar)
        self.input = Composer()
        self.input.textChanged.connect(self.save_draft)
        self.input.send_requested.connect(lambda: self.send_text(self.input.toPlainText(), "input"))
        right_layout.addWidget(self.input)
        self.send_button = QPushButton("发送文字（Enter）")
        self.send_button.clicked.connect(lambda: self.send_text(self.input.toPlainText(), "input"))
        right_layout.addWidget(self.send_button)
        self.phrase_widget = QWidget()
        self.phrase_layout = QVBoxLayout(self.phrase_widget)
        self.phrase_layout.setContentsMargins(0, 0, 0, 0)
        phrase_scroll = QScrollArea()
        phrase_scroll.setWidgetResizable(True)
        phrase_scroll.setWidget(self.phrase_widget)
        splitter.addWidget(right)
        phrase_panel = QWidget()
        phrase_panel.setMinimumWidth(180)
        phrase_panel_layout = QVBoxLayout(phrase_panel)
        phrase_panel_layout.addWidget(QLabel("快捷短句"))
        phrase_hint = QLabel("点击插入，或输入完整触发串立即替换；Enter 发送")
        phrase_hint.setWordWrap(True)
        phrase_panel_layout.addWidget(phrase_hint)
        phrase_panel_layout.addWidget(phrase_scroll, 1)
        splitter.addWidget(phrase_panel)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 650, 250])
        self.refresh_counts()

    def open_connection_settings(self):
        if self.closing:
            return
        if self.settings_dialog and self.settings_dialog.isVisible():
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
            return
        reason = ""
        if self.account_actions:
            reason = "请等待账号操作完成后再修改连接设置。"
        elif self.mock:
            reason = "模拟模式不使用 API 或代理；请关闭程序，不带 --mock 启动真实模式后修改。"
        elif self.pending_sends:
            reason = "请等待发送结束后再修改连接设置。"
        elif self.login_dialog and self.login_dialog.isVisible():
            reason = "请完成或取消登录，关闭登录窗口后再修改连接设置。"
        if reason:
            self.set_notice(reason)
            QMessageBox.information(self, "暂时无法修改连接设置", reason)
            return
        self.set_notice("正在打开连接设置…")
        dialog = SettingsDialog(self.store, self)
        self.settings_dialog = dialog
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.finished.connect(self.connection_settings_finished)
        dialog.adjustSize()
        dialog.move(self.frameGeometry().center() - dialog.rect().center())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def connection_settings_finished(self, result):
        dialog = self.settings_dialog
        self.settings_dialog = None
        if dialog:
            dialog.deleteLater()
        if result == QDialog.DialogCode.Accepted:
            self.reconnect()
        else:
            self.set_notice("已取消修改连接设置。")

    def reconnect(self):
        if (
            self.pending_sends
            or self.account_actions
            or (self.login_dialog and self.login_dialog.isVisible())
        ):
            self.set_notice("请等待发送完成并关闭登录窗口后重新连接。")
            return
        self.set_notice("正在重新连接…")
        self.bridge.submit("reconnect")

    def open_login(self):
        if self.account_actions:
            self.set_notice("请等待账号操作完成。")
            return
        if self.mock:
            self.set_notice("模拟模式提供五个示例账号，不进行真实登录。")
            return
        if not self.store.export_settings():
            self.open_connection_settings()
            return
        if self.pending_sends:
            self.set_notice("请等待发送结束后再登录。")
            return
        if self.login_dialog and self.login_dialog.isVisible():
            self.login_dialog.raise_()
            return
        self.login_dialog = LoginDialog(self.bridge, self)
        self.login_dialog.accepted.connect(self.login_finished)
        self.login_dialog.show()

    def login_finished(self):
        if self.login_dialog in self.hidden_dialogs:
            self.hidden_dialogs.remove(self.login_dialog)
        self.refresh_accounts()
        self.load_groups()

    def manage_phrases(self):
        if PhraseDialog(self.store, self).exec() == QDialog.DialogCode.Accepted:
            self.refresh_phrases()

    def account_menu(self, pos):
        item = self.accounts.itemAt(pos)
        if not item:
            return
        aid = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        logout = menu.addAction("退出登录")
        remove = menu.addAction("移除账号")
        action = menu.exec(self.accounts.mapToGlobal(pos))
        if action in (logout, remove):
            self.manage_account(aid, "logout" if action == logout else "remove_account")

    def manage_account(self, aid, operation):
        if (
            self.pending_sends
            or self.account_actions
            or (self.login_dialog and self.login_dialog.isVisible())
        ):
            self.set_notice("请等待发送或账号操作完成，并关闭登录窗口。")
            return
        account = next((a for a in self.store.accounts() if a["id"] == aid), None)
        if not account:
            return
        remove = operation == "remove_account"
        title = "移除账号" if remove else "退出登录"
        details = (
            "将退出本工具的登录，并删除该账号的本地会话、草稿、常用群、缓存及发送统计。不会注销 Telegram 账号。"
            if remove
            else "将退出本工具的登录，保留账号、草稿和发送统计；再次使用需要重新登录。"
        )
        if (
            QMessageBox.question(
                self,
                title,
                f"{account['label']}\n{details}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.save_view()
        self.account_actions.add(aid)
        self.bridge.submit(operation, account_id=aid)
        self.set_notice(f"正在{title}…")
        self.refresh_counts()

    def clear_account_view(self, aid):
        self.statuses.pop(aid, None)
        self.groups_cache.pop(aid, None)
        self.group_requests.pop(aid, None)
        for mapping in (
            self.buffers,
            self.cache.history,
            self.scroll_positions,
            self.history_requests,
            self.history_before,
        ):
            for target in list(mapping):
                if target[0] == aid:
                    mapping.pop(target)
        self.history_exhausted = {t for t in self.history_exhausted if t[0] != aid}
        self.resume_bottom = {t for t in self.resume_bottom if t[0] != aid}
        self.draft_dirty = {t for t in self.draft_dirty if t[0] != aid}
        if self.account_id == aid:
            # Do not save a removed account's draft while selecting the next account.
            self.account_id = None
            self.chat_id = None
            self.groups.blockSignals(True)
            self.groups.clear()
            self.groups.blockSignals(False)
            self.restore_draft()
            self.render_messages()

    def refresh_phrases(self):
        if not self.foreground:
            self.phrases_dirty = True
            return
        self.phrases_dirty = False
        while self.phrase_layout.count():
            item = self.phrase_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.phrases = self.store.phrases()
        if not self.phrases:
            self.phrase_layout.addWidget(QLabel("尚无短句，点击“管理短句”添加。"))
        self.input.phrase_prefix = self.store.phrase_prefix()
        self.input.phrase_aliases = {p["alias"]: p["text"] for p in self.phrases if p["alias"]}
        aliases = self.input.phrase_aliases
        conflicts = {
            alias
            for alias in aliases
            if any(
                alias != other and (alias.startswith(other) or other.startswith(alias))
                for other in aliases
            )
        }
        if conflicts:
            warning = QLabel(
                "部分旧缩写互为前缀，已暂停自动替换，请在“管理短句”修改："
                + "、".join(sorted(conflicts))
            )
            warning.setWordWrap(True)
            self.phrase_layout.addWidget(warning)
            self.input.phrase_aliases = {a: t for a, t in aliases.items() if a not in conflicts}
        for phrase in self.phrases:
            shortcut = self.input.phrase_prefix + phrase["alias"] + " · " if phrase["alias"] else ""
            label = shortcut + phrase["text"].replace("\n", " ")[:70]
            button = QPushButton(label.replace("&", "&&"))
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            button.setStyleSheet("text-align: left; padding-left: 6px;")
            button.setToolTip(shortcut + phrase["text"])
            button.clicked.connect(
                lambda checked=False, text=phrase["text"]: self.insert_phrase(text)
            )
            self.phrase_layout.addWidget(button)
        self.phrase_layout.addStretch()
        self.refresh_counts()

    def insert_phrase(self, text):
        if not self.account_id or self.chat_id is None or self.input.composing:
            return
        self.input.insertPlainText(text)
        self.input.setFocus()

    def refresh_accounts(self):
        selected = self.account_id
        accounts = self.store.accounts()
        self.account_records = {a["id"]: a for a in accounts}
        self.counts_dirty = True
        self.accounts_dirty = True
        if not self.foreground:
            return
        self.accounts_dirty = False
        self.accounts.blockSignals(True)
        self.accounts.clear()
        for account in accounts:
            item = QListWidgetItem(account["label"])
            item.setData(Qt.ItemDataRole.UserRole, account["id"])
            self.accounts.addItem(item)
            if selected == account["id"]:
                self.accounts.setCurrentItem(item)
        self.accounts.blockSignals(False)
        if selected and not any(a["id"] == selected for a in accounts):
            self.clear_account_view(selected)
        if not self.accounts.currentItem() and self.accounts.count():
            self.accounts.setCurrentRow(0)
        self.refresh_counts()

    def account_changed(self, current, previous):
        if self.closing or not current:
            return
        self.save_view()
        self.account_id = current.data(Qt.ItemDataRole.UserRole)
        self.chat_id = None
        self.search.clear()
        self.render_groups()
        self.restore_draft()
        self.render_messages()
        self.load_groups()

    def load_groups(self):
        if self.closing:
            return
        if self.account_id and self.statuses.get(self.account_id) in {"已连接", "模拟连接"}:
            request_id = self.bridge.submit("groups", account_id=self.account_id)
            self.group_requests[self.account_id] = request_id

    def render_groups(self):
        if not self.foreground:
            self.accounts_dirty = True
            return
        selected = self.chat_id
        self.groups.blockSignals(True)
        self.groups.clear()
        pins = self.store.pins(self.account_id)
        search = self.search.text().casefold()
        groups = sorted(
            self.groups_cache.get(self.account_id, []),
            key=lambda g: (g["id"] not in pins, g["title"].casefold()),
        )
        for group in groups:
            if search not in group["title"].casefold():
                continue
            item = QListWidgetItem(("★ " if group["id"] in pins else "") + group["title"])
            item.setData(Qt.ItemDataRole.UserRole, group["id"])
            self.groups.addItem(item)
            if group["id"] == selected:
                self.groups.setCurrentItem(item)
        self.groups.blockSignals(False)
        if self.chat_id is None and self.groups.count():
            account = next((a for a in self.store.accounts() if a["id"] == self.account_id), {})
            row = next(
                (
                    i
                    for i in range(self.groups.count())
                    if self.groups.item(i).data(Qt.ItemDataRole.UserRole)
                    == account.get("last_chat")
                ),
                0,
            )
            self.groups.setCurrentRow(row)

    def group_changed(self, current, previous):
        if self.closing or not current:
            return
        chat_id = current.data(Qt.ItemDataRole.UserRole)
        if chat_id == self.chat_id:
            return
        self.save_view()
        self.chat_id = chat_id
        missing = self.target not in self.buffers
        evicted = self.cache.open(self.target)
        if evicted is not None:
            self.scroll_positions.pop(evicted, None)
            self.history_requests.pop(evicted, None)
            self.history_before.pop(evicted, None)
            self.history_exhausted.discard(evicted)
            self.resume_bottom.discard(evicted)
            self.draft_dirty.discard(evicted)
        if missing:
            self.set_notice("已加载最近消息，可继续加载更早消息。")
        self.counts_dirty = True
        self.store.last_chat(self.account_id, chat_id)
        self.restore_draft()
        self.render_messages()
        self.load_history()

    def group_menu(self, pos):
        item = self.groups.itemAt(pos)
        if not item:
            return
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        action = menu.addAction("切换常用群置顶")
        if menu.exec(self.groups.mapToGlobal(pos)) == action:
            self.store.toggle_pin(self.account_id, chat_id)
            self.render_groups()

    def save_view(self):
        self.save_draft()
        if self.account_id and self.chat_id is not None:
            self.scroll_positions[self.target] = (
                self.messages.anchor() if self.target in self.cache.history else None
            )

    def save_draft(self):
        if self.target in self.draft_dirty:
            # A hidden failure has already restored the authoritative draft in SQLite.
            return
        if not self.loading_draft and self.account_id and self.chat_id is not None:
            self.store.save_draft(
                self.account_id,
                self.chat_id,
                self.input.toPlainText(),
                self.reply_id,
                self.reply_text,
            )

    def restore_draft(self):
        self.draft_dirty.discard(self.target)
        self.loading_draft = True
        draft = self.store.draft(*self.target) if self.chat_id is not None else {}
        self.input.setPlainText(draft.get("text", ""))
        self.reply_id = draft.get("reply_id")
        self.reply_text = draft.get("reply_text", "") or ""
        self.quote_label.setText(
            f"回复：{self.reply_text[:100]}" if self.reply_id else "双击消息引用回复"
        )
        self.loading_draft = False
        self.refresh_counts()

    def quote(self, item):
        message = item.data(MESSAGE_ROLE)
        if message:
            self.reply_id = message.message_id
            self.reply_text = f"{message.sender}：{message.text or media_text(message.media)}"
            self.quote_label.setText("回复：" + self.reply_text[:100])
            self.save_draft()
            self.input.setFocus()

    def cancel_quote(self):
        self.reply_id, self.reply_text = None, ""
        self.quote_label.setText("双击消息引用回复")
        self.save_draft()

    def message_menu(self, pos):
        item = self.messages.indexAt(pos)
        if item.isValid():
            menu = QMenu(self)
            action = menu.addAction("引用回复")
            if menu.exec(self.messages.viewport().mapToGlobal(pos)) == action:
                self.quote(item)

    def load_history(self, older=False):
        if self.closing or not self.account_id or self.chat_id is None:
            return
        if self.target in self.history_requests:
            return
        if older:
            self.cache.browse(self.target)
        buffer = self.cache.view(self.target)
        before = min(buffer) if older and buffer else 0
        request_id = self.bridge.submit(
            "history", account_id=self.account_id, chat_id=self.chat_id, before=before
        )
        self.history_requests[self.target] = request_id
        self.history_before[self.target] = before
        self.refresh_counts()

    @property
    def foreground(self):
        return self.isVisible() and not self.isMinimized() and not self.closing

    def put_message(self, message):
        target = message.account_id, message.chat_id
        self.cache.put(message)
        return target

    def render_messages(self, keep_scroll=False):
        if not self.foreground:
            self.ui_dirty = True
            return
        anchor = self.messages.anchor() if keep_scroll else self.scroll_positions.get(self.target)
        self.rendering = True
        self.message_sync_timer.stop()
        view = self.cache.view(self.target)
        self.messages.model().sync(self.target, view)
        self.messages.itemDelegate().prune(view.values())
        self.messages.doItemsLayout()
        self.messages.restore_anchor(anchor)
        if self.resize_pending:
            self.resize_anchor = anchor
        self.rendering = False
        self.ui_dirty = False

    def schedule_message_resize(self):
        if self.foreground:
            self.rendering = self.resize_rendering_state
            self.message_resize_timer.start(0)

    def prepare_message_resize(self):
        if not self.foreground:
            return
        if not self.resize_pending:
            bar = self.messages.verticalScrollBar()
            self.resize_anchor = self.messages.anchor() if bar.value() < bar.maximum() - 4 else None
        self.resize_pending = True
        # Geometry-driven scrollbar changes are not a manual history browse.
        self.resize_rendering_state = self.rendering
        self.rendering = True

    def resize_messages(self):
        if not self.foreground:
            return
        bar = self.messages.verticalScrollBar()
        anchor = (
            self.resize_anchor
            if self.resize_pending
            else (self.messages.anchor() if bar.value() < bar.maximum() - 4 else None)
        )
        self.rendering = True
        self.messages.itemDelegate().layouts.clear()
        self.messages.doItemsLayout()
        self.messages.restore_anchor(anchor)
        self.resize_pending = False
        self.rendering = False

    def scrolled(self):
        if not self.foreground or self.rendering:
            return
        bar = self.messages.verticalScrollBar()
        if bar.value() < bar.maximum() - 4:
            self.cache.browse(self.target)
            self.scroll_positions[self.target] = self.messages.anchor()
        if self.resize_pending:
            self.resize_anchor = self.messages.anchor() if bar.value() < bar.maximum() - 4 else None

    def go_bottom(self, refresh=True):
        if self.history_before.get(self.target):
            self.history_requests.pop(self.target, None)
            self.history_before.pop(self.target, None)
        self.cache.bottom(self.target)
        self.scroll_positions[self.target] = None
        if not self.foreground:
            self.resume_bottom.add(self.target)
            self.ui_dirty = True
            return
        self.render_messages()
        self.bottom.setText("回到底部")
        self.history_exhausted.discard(self.target)
        if refresh:
            self.load_history()

    def pause_view(self):
        if not self.closing:
            self.save_view()
        if hasattr(self, "timer"):
            self.timer.stop()
        self.message_resize_timer.stop()
        self.message_sync_timer.stop()
        self.resize_pending = False
        self.messages.setUpdatesEnabled(False)

    def resume_view(self):
        if not self.foreground:
            return
        self.messages.setUpdatesEnabled(True)
        if self.accounts_dirty:
            self.refresh_accounts()
            self.render_groups()
        if self.phrases_dirty:
            self.refresh_phrases()
        if self.target in self.resume_bottom:
            self.resume_bottom.discard(self.target)
            self.cache.bottom(self.target)
            self.scroll_positions[self.target] = None
        had_updates = self.ui_dirty
        self.render_messages()
        if self.target in self.draft_dirty:
            self.restore_draft()
        if self.notice_text is not None:
            self.notice.setText(self.notice_text)
        if self.target in self.cache.history:
            self.bottom.setText("有新消息 · 回到底部" if had_updates else "回到底部")
        self.refresh_counts()

    def set_notice(self, text):
        self.notice_text = text
        if self.foreground:
            self.notice.setText(text)

    def showEvent(self, event):
        super().showEvent(event)
        self.resume_view()

    def hideEvent(self, event):
        self.pause_view()
        super().hideEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self.pause_view()
            else:
                self.resume_view()

    def schedule_midnight(self):
        now = datetime.now(BEIJING)
        tomorrow = datetime(now.year, now.month, now.day, tzinfo=BEIJING) + timedelta(days=1)
        self.day_timer.start(max(1, int((tomorrow - now).total_seconds() * 1000) + 100))

    def day_changed(self):
        self.counts_dirty = True
        self.refresh_counts()
        self.schedule_midnight()

    def send_text(self, text, source):
        if self.account_actions or self.statuses.get(self.account_id) not in {"已连接", "模拟连接"}:
            self.set_notice("账号未连接或正在管理账号，请连接并登录后再发送。")
            return
        if not self.account_id or self.chat_id is None:
            self.set_notice("请先选择账号和群。")
            return
        deadline = max(
            self.blocked.get(self.target, 0), self.blocked_accounts.get(self.account_id, 0)
        )
        if self.target in self.pending_sends:
            self.set_notice("上一条仍在发送，当前草稿已保留；发送结束后请再按 Enter。")
            return
        if deadline > time.monotonic():
            self.set_notice("Telegram 等待时间尚未结束，当前草稿已保留。")
            return
        if not text.strip() or len(text) > 4096:
            self.set_notice("消息须为 1–4096 个字符。")
            return
        self.save_draft()
        request = SendRequest(uuid4().hex, self.account_id, self.chat_id, text, self.reply_id)
        self.bridge.submit("send", request=request)
        self.pending_sends[self.target] = (request, source, self.reply_text)
        if source == "input":
            self.reply_id, self.reply_text = None, ""
            self.quote_label.setText("双击消息引用回复")
            self.input.clear()
            self.save_draft()
        self.set_notice("正在发送；账号和目标群已固定。")
        self.refresh_counts()

    def refresh_counts(self):
        if not self.foreground:
            self.ui_dirty = True
            if hasattr(self, "timer"):
                self.timer.stop()
            return
        if self.count_day != day_key():
            self.counts_dirty = True
        if self.counts_dirty:
            self.count_day = day_key()
            self.counts_snapshot = self.store.counts_for_accounts(
                self.account_records, self.count_day
            )
            self.counts_dirty = False
        accounts = self.account_records
        totals = {}
        for (aid, _), total in self.counts_snapshot.items():
            totals[aid] = totals.get(aid, 0) + total
        for i in range(self.accounts.count()):
            item = self.accounts.item(i)
            aid = item.data(Qt.ItemDataRole.UserRole)
            account = accounts.get(aid)
            if account is None:
                continue
            shortcut = f"Alt+{i + 1} · " if i < 9 else ""
            item.setText(
                f"{shortcut}{account['label']} · {totals.get(aid, 0)} 条\n"
                f"{self.statuses.get(aid, '未连接')}"
            )
        valid = bool(self.account_id and self.chat_id is not None)
        deadline = max(
            self.blocked.get(self.target, 0), self.blocked_accounts.get(self.account_id, 0)
        )
        wait = max(0, ceil(deadline - time.monotonic()))
        if hasattr(self, "timer"):
            if wait:
                if not self.timer.isActive():
                    self.timer.start(1000)
            else:
                self.timer.stop()
        busy = self.target in self.pending_sends
        online = self.statuses.get(self.account_id) in {"已连接", "模拟连接"}
        self.send_button.setEnabled(
            valid and online and not self.account_actions and not busy and not wait
        )
        self.phrase_widget.setEnabled(valid)
        self.input.setEnabled(valid)
        self.older.setEnabled(
            valid
            and self.target not in self.history_requests
            and self.target not in self.history_exhausted
        )
        group = next(
            (
                g["title"]
                for g in self.groups_cache.get(self.account_id, [])
                if g["id"] == self.chat_id
            ),
            "未选群",
        )
        label = accounts.get(self.account_id, {}).get("label", "未选账号")
        count = self.counts_snapshot.get(self.target, 0) if valid else 0
        self.identity.setText(
            f"当前账号：{label}　→　{group}　|　本群今日工具发送：{count} 条"
            + (f"　等待 {wait} 秒" if wait else "")
        )
        self.send_button.setText("发送中…" if busy else "发送文字（Enter）")

    def handle(self, event):
        if self.closing:
            return
        kind = event["kind"]
        operation = event.get("operation")
        aid = event.get("account_id")
        if operation in {"logout", "remove_account"}:
            self.account_actions.discard(aid)
            if kind == "result":
                self.clear_account_view(aid)
                if operation == "logout":
                    self.statuses[aid] = "需要登录"
                self.refresh_accounts()
                self.set_notice(
                    "账号已移除。"
                    if operation == "remove_account"
                    else "已退出登录，草稿和统计已保留。"
                )
            else:
                self.set_notice(event["message"])
            self.refresh_counts()
            return
        if aid and aid not in self.account_records and kind != "status":
            return
        if kind == "status":
            if aid not in self.account_records:
                self.refresh_accounts()
                if aid not in self.account_records:
                    return
            self.statuses[event["account_id"]] = event["status"]
            self.refresh_counts()
            return
        if kind == "message":
            message = event["message"]
            if (
                message.account_id not in self.account_records
                or message.account_id in self.account_actions
            ):
                return
            target = self.put_message(message)
            if target == self.target:
                if target not in self.cache.history:
                    if self.foreground:
                        if not self.message_sync_timer.isActive():
                            self.message_sync_timer.start(16)
                    else:
                        self.ui_dirty = True
                else:
                    if self.foreground:
                        self.bottom.setText("有新消息 · 回到底部")
                    self.ui_dirty = True
            return
        if (
            self.login_dialog
            and (self.login_dialog.isVisible() or self.login_dialog in self.hidden_dialogs)
            and self.login_dialog.handle(event)
        ):
            self.refresh_accounts()
            return
        if operation == "send":
            request = event["request"]
            target = request.account_id, request.chat_id
            pending = self.pending_sends.get(target)
            if pending and pending[0].request_id != request.request_id:
                return
            self.pending_sends.pop(target, None)
            if kind == "result":
                self.counts_dirty = True
                if pending and pending[1] != "input":
                    self.store.complete_draft(request, pending[1])
                self.put_message(event["result"])
                if target == self.target:
                    if pending and pending[1] != "input":
                        self.restore_draft()
                    self.go_bottom(refresh=False)
                self.set_notice("发送成功。")
            else:
                if pending and pending[1] == "input":
                    draft = self.store.draft(*target)
                    text = request.text + ("\n" + draft["text"] if draft["text"] else "")
                    self.store.save_draft(
                        *target,
                        text,
                        draft["reply_id"]
                        if draft["text"] or draft["reply_id"] is not None
                        else request.reply_to,
                        draft["reply_text"]
                        if draft["text"] or draft["reply_id"] is not None
                        else pending[2],
                    )
                    if target == self.target and self.foreground:
                        self.restore_draft()
                    elif target == self.target:
                        self.draft_dirty.add(target)
                if event.get("wait"):
                    if event.get("wait_scope") == "account":
                        self.blocked_accounts[request.account_id] = time.monotonic() + event["wait"]
                    else:
                        self.blocked[target] = time.monotonic() + event["wait"]
                self.set_notice(event["message"])
                if pending and pending[1] == "input":
                    self.set_notice(
                        event["message"] + "；原文本已恢复到该聊天草稿开头，请核对后手动发送。"
                    )
        elif operation == "groups":
            aid = event["account_id"]
            if event["request_id"] != self.group_requests.get(aid):
                return
            if kind == "result":
                self.groups_cache[aid] = event["result"]
                if aid == self.account_id:
                    self.render_groups()
            else:
                self.set_notice(event["message"])
        elif operation == "history":
            target = event["account_id"], event["chat_id"]
            if self.history_requests.get(target) != event["request_id"]:
                return
            self.history_requests.pop(target, None)
            self.history_before.pop(target, None)
            if kind == "result":
                if not event["result"]:
                    self.history_exhausted.add(target)
                if event.get("before"):
                    self.cache.older(target, event["result"])
                else:
                    for message in event["result"]:
                        self.put_message(message)
                if target == self.target:
                    self.render_messages(keep_scroll=bool(event.get("before")))
            else:
                self.set_notice(event["message"])
        elif operation in {"start", "reconnect"}:
            self.refresh_accounts()
            self.load_groups()
            if operation == "start" and kind == "result":
                self.refresh_phrases()
            if kind == "result":
                self.set_notice(
                    "模拟服务已启动。" if self.mock else "连接检查完成，请查看各账号状态。"
                )
            else:
                self.set_notice(event["message"])
        elif kind == "error":
            self.set_notice(event["message"])
        self.refresh_counts()

    def setup_tray(self):
        # Draw the icon locally so frozen builds need no external image resources.
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2386c8"))
        painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
        painter.setBrush(QColor("white"))
        painter.drawPolygon(
            QPolygon(
                [
                    QPoint(12, 28),
                    QPoint(52, 13),
                    QPoint(39, 51),
                    QPoint(29, 36),
                    QPoint(22, 43),
                    QPoint(22, 32),
                ]
            )
        )
        painter.end()
        self.setWindowIcon(QIcon(pixmap))
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("TG Chat Client — " + ("模拟模式" if self.mock else "真实模式"))
        self.tray_menu = QMenu(self)
        self.tray_menu.addAction("显示主窗口", self.restore_window)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(self.exit_action)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()

    def tray_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.restore_window()

    def restore_window(self):
        if self.closing:
            return
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()
        dialogs, self.hidden_dialogs = self.hidden_dialogs, []
        for dialog in dialogs:
            if dialog in self.findChildren(QDialog):
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()

    def request_exit(self):
        if self.closing:
            return
        self.shutdown()
        self.close()
        self.quit_requested.emit()

    def eventFilter(self, watched, event):
        # QApplication.quit() closes windows before aboutToQuit. Mark a real exit
        # first, otherwise closeEvent would turn that request into a tray hide.
        if watched is QApplication.instance() and event.type() == QEvent.Type.Quit:
            self.shutdown()
        return super().eventFilter(watched, event)

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.save_view()
        for dialog in self.findChildren(QDialog):
            dialog.hide()
        self.hidden_dialogs.clear()
        self.timer.stop()
        self.day_timer.stop()
        self.message_resize_timer.stop()
        self.message_sync_timer.stop()
        self.bridge.event.disconnect(self.handle)
        QApplication.instance().removeEventFilter(self.shortcut_filter)
        if self.tray:
            self.tray.hide()
        self.bridge.close()

    def closeEvent(self, event):
        if self.closing:
            event.accept()
            return
        if self.tray and QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            if not self.store.setting("tray_hint_seen", False):
                QMessageBox.information(
                    self,
                    "程序仍在运行",
                    "关闭窗口后账号继续在线。\n点击右下角托盘图标恢复窗口；"
                    "右键托盘图标选择“退出”，或通过“程序 → 退出”结束程序。",
                )
                self.store.set_setting("tray_hint_seen", True)
                if self.closing:
                    event.accept()
                    return
            self.save_view()
            self.hidden_dialogs = [
                d for d in self.findChildren(QDialog) if d.isVisible() or d in self.hidden_dialogs
            ]
            for dialog in self.hidden_dialogs:
                dialog.hide()
            self.hide()
            return
        self.shutdown()
        if self.tray:
            self.quit_requested.emit()
        event.accept()


class ShortcutFilter(QObject):
    """Application filter reaches focused text fields, but never acts in other apps/dialogs."""

    def __init__(self, window):
        # A QWidget child would show over the top-left menu bar and intercept clicks.
        super().__init__(window)
        self.window = window

    def eventFilter(self, watched, event):
        if isinstance(event, QKeyEvent) and event.type() == event.Type.KeyPress:
            window = self.window
            if QApplication.activeWindow() is not window:
                return False
            index = event.key() - Qt.Key.Key_1
            modifiers = event.modifiers()
            if 0 <= index < 9:
                if window.input.composing and modifiers == Qt.KeyboardModifier.AltModifier:
                    return True
                if modifiers == Qt.KeyboardModifier.AltModifier:
                    if not event.isAutoRepeat() and index < window.accounts.count():
                        window.accounts.setCurrentRow(index)
                    return True
        return False
