import logging
import re
import time
from dataclasses import replace
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QInputMethodEvent, QKeyEvent, QPixmap, QTextCursor
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .models import ProxyConfig, SendRequest
from .native_ime import cancel_windows_composition
from .store import validate_phrase_alias

logger = logging.getLogger(__name__)


class Composer(QTextEdit):
    send_requested = Signal()

    def __init__(self):
        super().__init__()
        self.composing = False
        self.resetting_input_method = False
        self.ime_reset_timer = QTimer(self)
        self.ime_reset_timer.setSingleShot(True)
        self.ime_reset_timer.timeout.connect(self.finish_ime_reset)
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
    def __init__(self, store, bridge, mock=False):
        super().__init__()
        self.store, self.bridge, self.mock = store, bridge, mock
        self.account_id = None
        self.chat_id = None
        self.groups_cache = {}
        self.buffers = {}
        self.items = {}
        self.statuses = {}
        self.pending_sends = {}
        self.blocked = {}
        self.blocked_accounts = {}
        self.group_requests = {}
        self.history_requests = {}
        self.preview_requested = set()
        self.login_dialog = None
        self.settings_dialog = None
        self.reply_id = None
        self.reply_text = ""
        self.loading_draft = False
        self.rendering = False
        self.scroll_positions = {}
        self.history_exhausted = set()
        self.setWindowTitle("TG Chat Client" + (" — 模拟模式（不会连接 Telegram）" if mock else ""))
        self.resize(1160, 780)
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.timeout.connect(self.visible_previews)
        self.build_ui()
        self.bridge.event.connect(self.handle)
        self.refresh_accounts()
        self.refresh_phrases()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_counts)
        self.timer.start(1000)
        self.shortcut_filter = ShortcutFilter(self)
        QApplication.instance().installEventFilter(self.shortcut_filter)
        QTimer.singleShot(0, lambda: self.bridge.submit("start"))
        if not mock and not store.export_settings():
            QTimer.singleShot(100, self.open_connection_settings)

    @property
    def target(self):
        return self.account_id, self.chat_id

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        toolbar = QHBoxLayout()
        self.settings_button = QPushButton("连接设置")
        self.settings_button.pressed.connect(lambda: logger.info("Connection settings pressed"))
        self.settings_button.clicked.connect(lambda: self.open_connection_settings())
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
        self.bottom.clicked.connect(self.go_bottom)
        history_bar.addWidget(self.bottom)
        right_layout.addLayout(history_bar)
        self.messages = QListWidget()
        self.messages.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.messages.itemDoubleClicked.connect(self.quote)
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
        logger.info("Connection settings requested")
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
            logger.info("Connection settings blocked")
            self.notice.setText(reason)
            QMessageBox.information(self, "暂时无法修改连接设置", reason)
            return
        self.notice.setText("正在打开连接设置…")
        dialog = SettingsDialog(self.store, self)
        self.settings_dialog = dialog
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.finished.connect(self.connection_settings_finished)
        dialog.adjustSize()
        dialog.move(self.frameGeometry().center() - dialog.rect().center())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        logger.info("Connection settings shown")

    def connection_settings_finished(self, result):
        dialog = self.settings_dialog
        self.settings_dialog = None
        if dialog:
            dialog.deleteLater()
        logger.info("Connection settings finished: %s", result)
        if result == QDialog.DialogCode.Accepted:
            self.reconnect()
        else:
            self.notice.setText("已取消修改连接设置。")

    def reconnect(self):
        if (
            self.pending_sends
            or self.account_actions
            or (self.login_dialog and self.login_dialog.isVisible())
        ):
            self.notice.setText("请等待发送完成并关闭登录窗口后重新连接。")
            return
        self.notice.setText("正在重新连接…")
        self.bridge.submit("reconnect")

    def open_login(self):
        if self.account_actions:
            self.notice.setText("请等待账号操作完成。")
            return
        if self.mock:
            self.notice.setText("模拟模式提供五个示例账号，不进行真实登录。")
            return
        if not self.store.export_settings():
            self.open_connection_settings()
            return
        if self.pending_sends:
            self.notice.setText("请等待发送结束后再登录。")
            return
        if self.login_dialog and self.login_dialog.isVisible():
            self.login_dialog.raise_()
            return
        self.login_dialog = LoginDialog(self.bridge, self)
        self.login_dialog.accepted.connect(self.login_finished)
        self.login_dialog.show()

    def login_finished(self):
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
            self.notice.setText("请等待发送或账号操作完成，并关闭登录窗口。")
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
        self.notice.setText(f"正在{title}…")
        self.refresh_counts()

    def clear_account_view(self, aid):
        self.statuses.pop(aid, None)
        self.groups_cache.pop(aid, None)
        self.group_requests.pop(aid, None)
        for mapping in (self.buffers, self.scroll_positions, self.history_requests):
            for target in list(mapping):
                if target[0] == aid:
                    mapping.pop(target)
        self.history_exhausted = {t for t in self.history_exhausted if t[0] != aid}
        self.preview_requested = {t for t in self.preview_requested if t[0] != aid}
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
        if not current:
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
        if self.account_id and self.statuses.get(self.account_id) in {"已连接", "模拟连接"}:
            request_id = self.bridge.submit("groups", account_id=self.account_id)
            self.group_requests[self.account_id] = request_id

    def render_groups(self):
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
        if not current:
            return
        chat_id = current.data(Qt.ItemDataRole.UserRole)
        if chat_id == self.chat_id:
            return
        self.save_view()
        self.chat_id = chat_id
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
            bar = self.messages.verticalScrollBar()
            self.scroll_positions[self.target] = (
                None if bar.value() >= bar.maximum() - 4 else bar.value()
            )

    def save_draft(self):
        if not self.loading_draft and self.account_id and self.chat_id is not None:
            self.store.save_draft(
                self.account_id,
                self.chat_id,
                self.input.toPlainText(),
                self.reply_id,
                self.reply_text,
            )

    def restore_draft(self):
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
        message = self.buffers.get(self.target, {}).get(item.data(Qt.ItemDataRole.UserRole))
        if message:
            self.reply_id = message.message_id
            self.reply_text = f"{message.sender}：{message.text or '[' + message.media + ']'}"
            self.quote_label.setText("回复：" + self.reply_text[:100])
            self.save_draft()
            self.input.setFocus()

    def cancel_quote(self):
        self.reply_id, self.reply_text = None, ""
        self.quote_label.setText("双击消息引用回复")
        self.save_draft()

    def message_menu(self, pos):
        item = self.messages.itemAt(pos)
        if item:
            menu = QMenu(self)
            action = menu.addAction("引用回复")
            if menu.exec(self.messages.mapToGlobal(pos)) == action:
                self.quote(item)

    def load_history(self, older=False):
        if not self.account_id or self.chat_id is None:
            return
        if self.target in self.history_requests:
            return
        buffer = self.buffers.get(self.target, {})
        before = min(buffer) if older and buffer else 0
        request_id = self.bridge.submit(
            "history", account_id=self.account_id, chat_id=self.chat_id, before=before
        )
        self.history_requests[self.target] = request_id
        self.refresh_counts()

    def put_message(self, message):
        target = message.account_id, message.chat_id
        buffer = self.buffers.setdefault(target, {})
        existing = buffer.get(message.message_id)
        if existing and existing.preview_path:
            message = replace(message, preview_path=existing.preview_path)
        buffer[message.message_id] = message
        return target

    def render_messages(self, keep_scroll=False):
        self.rendering = True
        bar = self.messages.verticalScrollBar()
        old_value = bar.value()
        old_max = bar.maximum()
        stick_bottom = old_value >= old_max - 4
        self.messages.clear()
        self.items.clear()
        for message in sorted(
            self.buffers.get(self.target, {}).values(), key=lambda m: m.message_id
        ):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, message.message_id)
            if message.outgoing:
                item.setBackground(QColor("#e7f3ed"))
            self.messages.addItem(item)
            self.items[message.message_id] = item
            self.render_message(item, message)
        if keep_scroll:
            if stick_bottom:
                self.messages.scrollToBottom()
            else:
                bar.setValue(old_value + max(0, bar.maximum() - old_max))
        elif self.scroll_positions.get(self.target) is None:
            self.messages.scrollToBottom()
        else:
            bar.setValue(self.scroll_positions[self.target])
        self.rendering = False
        self.preview_timer.start(100)

    def render_message(self, item, message):
        if message.outgoing:
            item.setBackground(QColor("#e7f3ed"))
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 5, 8, 5)
        header = QLabel(
            f"{message.sender} · {message.time}"
            + (f" · 回复 #{message.reply_to}" if message.reply_to else "")
        )
        header.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(header)
        if message.text:
            text = QLabel(message.text)
            text.setTextFormat(Qt.TextFormat.PlainText)
            text.setWordWrap(True)
            text.setMaximumWidth(max(300, self.messages.viewport().width() - 40))
            layout.addWidget(text)
        if message.media:
            preview = QPixmap(message.preview_path) if message.preview_path else QPixmap()
            label = QLabel()
            if not preview.isNull():
                label.setPixmap(
                    preview.scaled(
                        260,
                        200,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            else:
                names = {
                    "photo": "图片",
                    "sticker": "贴纸",
                    "video": "视频",
                    "audio": "音频",
                    "file": "文件",
                }
                label.setText(
                    "["
                    + names.get(message.media, "其他媒体")
                    + "]"
                    + (" 预览加载中或暂不可用" if message.media in {"photo", "sticker"} else "")
                )
            layout.addWidget(label)
        self.messages.setItemWidget(item, widget)
        item.setSizeHint(widget.sizeHint())

    def scrolled(self):
        if not self.rendering:
            self.preview_timer.start(100)

    def visible_previews(self):
        viewport = self.messages.viewport().rect()
        for mid, item in self.items.items():
            message = self.buffers.get(self.target, {}).get(mid)
            key = (*self.target, mid)
            if (
                message
                and message.media in {"photo", "sticker"}
                and not message.preview_path
                and key not in self.preview_requested
                and self.messages.visualItemRect(item).intersects(viewport)
            ):
                self.preview_requested.add(key)
                self.bridge.submit(
                    "preview", account_id=self.account_id, chat_id=self.chat_id, message_id=mid
                )

    def go_bottom(self):
        self.messages.scrollToBottom()
        self.bottom.setText("回到底部")

    def send_text(self, text, source):
        if self.account_actions or self.statuses.get(self.account_id) not in {"已连接", "模拟连接"}:
            self.notice.setText("账号未连接或正在管理账号，请连接并登录后再发送。")
            return
        if not self.account_id or self.chat_id is None:
            self.notice.setText("请先选择账号和群。")
            return
        deadline = max(
            self.blocked.get(self.target, 0), self.blocked_accounts.get(self.account_id, 0)
        )
        if self.target in self.pending_sends or deadline > time.monotonic():
            return
        if not text.strip() or len(text) > 4096:
            self.notice.setText("消息须为 1–4096 个字符。")
            return
        self.save_draft()
        request = SendRequest(uuid4().hex, self.account_id, self.chat_id, text, self.reply_id)
        self.pending_sends[self.target] = (request, source)
        self.bridge.submit("send", request=request)
        self.notice.setText("正在发送；账号和目标群已固定。")
        self.refresh_counts()

    def refresh_counts(self):
        accounts = {a["id"]: a for a in self.store.accounts()}
        for i in range(self.accounts.count()):
            item = self.accounts.item(i)
            aid = item.data(Qt.ItemDataRole.UserRole)
            account = accounts.get(aid)
            if account is None:
                continue
            shortcut = f"Alt+{i + 1} · " if i < 9 else ""
            item.setText(
                f"{shortcut}{account['label']} · {self.store.count(aid)} 条\n"
                f"{self.statuses.get(aid, '未连接')}"
            )
        valid = bool(self.account_id and self.chat_id is not None)
        deadline = max(
            self.blocked.get(self.target, 0), self.blocked_accounts.get(self.account_id, 0)
        )
        wait = max(0, int(deadline - time.monotonic()) + 1)
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
        count = self.store.count(*self.target) if valid else 0
        self.identity.setText(
            f"当前账号：{label}　→　{group}　|　本群今日工具发送：{count} 条"
            + (f"　等待 {wait} 秒" if wait else "")
        )
        self.send_button.setText("发送中…" if busy else "发送文字（Enter）")

    def handle(self, event):
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
                self.notice.setText(
                    "账号已移除。"
                    if operation == "remove_account"
                    else "已退出登录，草稿和统计已保留。"
                )
            else:
                self.notice.setText(event["message"])
            self.refresh_counts()
            return
        if aid and not any(a["id"] == aid for a in self.store.accounts()):
            return
        if kind == "status":
            self.statuses[event["account_id"]] = event["status"]
            self.refresh_counts()
            return
        if kind == "message":
            message = event["message"]
            if (
                not any(a["id"] == message.account_id for a in self.store.accounts())
                or message.account_id in self.account_actions
            ):
                return
            target = self.put_message(message)
            if target == self.target:
                bar = self.messages.verticalScrollBar()
                at_bottom = bar.value() >= bar.maximum() - 4
                self.append_visible(message)
                if at_bottom:
                    self.messages.scrollToBottom()
                else:
                    self.bottom.setText("有新消息 · 回到底部")
            return
        if self.login_dialog and self.login_dialog.isVisible() and self.login_dialog.handle(event):
            self.refresh_accounts()
            return
        if operation == "send":
            request = event["request"]
            target = request.account_id, request.chat_id
            pending = self.pending_sends.pop(target, None)
            if kind == "result":
                self.store.complete_draft(request, pending[1] if pending else "phrase")
                self.put_message(event["result"])
                if target == self.target:
                    self.restore_draft()
                    self.append_visible(event["result"])
                    self.go_bottom()
                self.notice.setText("发送成功。")
            else:
                if event.get("wait"):
                    if event.get("wait_scope") == "account":
                        self.blocked_accounts[request.account_id] = time.monotonic() + event["wait"]
                    else:
                        self.blocked[target] = time.monotonic() + event["wait"]
                self.notice.setText(event["message"])
        elif operation == "groups":
            aid = event["account_id"]
            if event["request_id"] != self.group_requests.get(aid):
                return
            if kind == "result":
                self.groups_cache[aid] = event["result"]
                if aid == self.account_id:
                    self.render_groups()
            else:
                self.notice.setText(event["message"])
        elif operation == "history":
            target = event["account_id"], event["chat_id"]
            if self.history_requests.get(target) != event["request_id"]:
                return
            self.history_requests.pop(target, None)
            if kind == "result":
                if not event["result"]:
                    self.history_exhausted.add(target)
                for message in event["result"]:
                    self.put_message(message)
                if target == self.target:
                    self.render_messages(keep_scroll=bool(event.get("before")))
            else:
                self.notice.setText(event["message"])
        elif operation == "preview":
            target = event["account_id"], event["chat_id"]
            mid = event["message_id"]
            message = self.buffers.get(target, {}).get(mid)
            if kind == "result" and event["result"] and message:
                updated = replace(message, preview_path=event["result"])
                self.buffers[target][mid] = updated
                if target == self.target and mid in self.items:
                    self.render_message(self.items[mid], updated)
                    self.preview_timer.start(100)
        elif operation in {"start", "reconnect"}:
            self.refresh_accounts()
            self.load_groups()
            if kind == "result":
                self.notice.setText(
                    "模拟服务已启动。" if self.mock else "连接检查完成，请查看各账号状态。"
                )
            else:
                self.notice.setText(event["message"])
        elif kind == "error":
            self.notice.setText(event["message"])
        self.refresh_counts()

    def append_visible(self, message):
        if message.message_id not in self.items:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, message.message_id)
            self.messages.addItem(item)
            self.items[message.message_id] = item
        self.render_message(
            self.items[message.message_id], self.buffers[self.target][message.message_id]
        )
        self.preview_timer.start(100)

    def closeEvent(self, event):
        self.save_view()
        self.timer.stop()
        self.preview_timer.stop()
        QApplication.instance().removeEventFilter(self.shortcut_filter)
        self.bridge.close()
        event.accept()


class ShortcutFilter(QWidget):
    """Application filter reaches focused text fields, but never acts in other apps/dialogs."""

    def __init__(self, window):
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
