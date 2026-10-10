from dataclasses import replace

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QInputMethodEvent, QKeyEvent, QTextCursor
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QSystemTrayIcon

from tg_chat_client.bridge import ServiceBridge
from tg_chat_client.models import SendRequest
from tg_chat_client.store import Store
from tg_chat_client.ui import Composer, MainWindow


def test_tray_close_preserves_draft_quote_and_live_service(tray_window, qtbot):
    window = tray_window
    target = window.target
    window.input.setPlainText("托盘草稿")
    window.quote(window.messages.model().index(0))
    reply = window.reply_id
    bar = window.messages.verticalScrollBar()
    bar.setValue(0)
    window.close()
    assert not window.isVisible()
    assert not window.closing and not window.bridge.closed
    assert not window.timer.isActive()
    assert window.store.draft(*target)["text"] == "托盘草稿"
    assert window.store.draft(*target)["reply_id"] == reply
    window.tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
    qtbot.waitUntil(window.isVisible)
    assert window.target == target
    assert window.input.toPlainText() == "托盘草稿"
    assert window.reply_id == reply
    assert bar.value() == 0
    assert not window.pending_sends
    assert not window.windowIcon().isNull()
    assert "模拟模式" in window.tray.toolTip()


def test_tray_background_send_completes_without_clearing_next_draft(tray_window, qtbot):
    window = tray_window
    window.input.setPlainText("提交到托盘")
    window.send_text(window.input.toPlainText(), "input")
    assert window.pending_sends
    window.input.setPlainText("下一条草稿")
    window.close()
    qtbot.waitUntil(lambda: not window.pending_sends)
    assert not window.closing and not window.bridge.closed
    assert not window.isVisible()
    window.restore_window()
    assert window.input.toPlainText() == "下一条草稿"
    assert any(m.text == "提交到托盘" for m in window.buffers[window.target].values())


def test_tray_restores_owned_dialog_without_accepting_it(tray_window):
    window = tray_window
    dialog = QDialog(window)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.show()
    finished = []
    dialog.finished.connect(finished.append)
    window.close()
    assert not dialog.isVisible()
    assert not finished
    window.close()
    window.showMinimized()
    window.tray.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)
    assert window.isVisible() and not window.isMinimized()
    assert dialog.isVisible()
    assert not finished
    dialog.reject()


def test_tray_first_close_hint_is_only_shown_once(tray_window, monkeypatch):
    window = tray_window
    window.store.set_setting("tray_hint_seen", False)
    hints = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: hints.append(args[2]))
    window.close()
    window.restore_window()
    window.close()
    assert len(hints) == 1 and "退出" in hints[0]
    assert window.store.setting("tray_hint_seen")


def test_login_result_while_hidden_does_not_reopen_completed_dialog(tray_window):
    window = tray_window

    class LoginResultDialog(QDialog):
        def handle(self, event):
            self.accept()
            return True

    dialog = LoginResultDialog(window)
    window.login_dialog = dialog
    dialog.accepted.connect(window.login_finished)
    dialog.show()
    window.close()
    window.handle({"kind": "result", "operation": "sign_in", "result": {}})
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog not in window.hidden_dialogs
    window.restore_window()
    assert not dialog.isVisible()


def test_tray_exit_cleans_up_once(tray_window, monkeypatch):
    window = tray_window
    calls = []
    original_close = window.bridge.close

    def close():
        calls.append("close")
        original_close()

    monkeypatch.setattr(window.bridge, "close", close)
    quit_requests = []
    window.quit_requested.connect(lambda: quit_requests.append(True))
    window.tray_menu.actions()[-1].trigger()
    window.request_exit()
    assert window.closing and window.bridge.closed
    assert not window.timer.isActive()
    assert not window.message_resize_timer.isActive()
    assert not window.tray.isVisible()
    assert calls == ["close"]
    assert quit_requests == [True]


def test_close_without_tray_exits(window):
    assert window.tray is None
    window.close()
    assert window.closing and window.bridge.closed


def test_close_when_tray_becomes_unavailable_exits(tray_window, monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    tray_window.close()
    assert tray_window.closing and tray_window.bridge.closed


def test_first_run_settings_can_reopen_after_saving_without_accounts(qtbot, tmp_path):
    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtWidgets import QPushButton

    from tg_chat_client.ui import SettingsDialog

    class OfflineBridge(QObject):
        event = Signal(object)

        def __init__(self):
            super().__init__()
            self.calls = []

        def submit(self, operation, **args):
            self.calls.append(operation)

        def close(self):
            pass

    store = Store(tmp_path)
    bridge = OfflineBridge()
    window = MainWindow(store, bridge)
    qtbot.addWidget(window)
    window.show()

    def save_first_settings():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, SettingsDialog)
        dialog.api_id.setText("123")
        dialog.api_hash.setText("test-only-hash")
        dialog.port.setValue(7892)
        dialog.save()

    QTimer.singleShot(150, save_first_settings)
    qtbot.waitUntil(lambda: "reconnect" in bridge.calls)
    assert window.accounts.count() == 0

    button = next(b for b in window.findChildren(QPushButton) if b.text() == "连接设置")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    dialog = window.settings_dialog
    assert isinstance(dialog, SettingsDialog)
    assert dialog.isVisible()
    assert dialog.port.value() == 7892
    dialog.reject()
    window.request_exit()


def test_connection_settings_button_reopens_and_updates_proxy(window, qtbot, monkeypatch):
    from PySide6.QtWidgets import QPushButton

    from tg_chat_client.models import ProxyConfig
    from tg_chat_client.ui import SettingsDialog

    window.store.save_connection(123, "test-only-hash", ProxyConfig(port=7892))
    window.mock = False
    calls = []
    monkeypatch.setattr(window.bridge, "submit", lambda op, **kw: calls.append(op))
    button = next(b for b in window.findChildren(QPushButton) if b.text() == "连接设置")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    dialog = window.settings_dialog
    assert isinstance(dialog, SettingsDialog)
    assert dialog.isVisible()
    assert dialog.port.value() == 7892
    dialog.port.setValue(7893)
    dialog.save()
    assert window.store.export_settings()["proxy"]["port"] == 7893
    assert calls == ["reconnect"]

    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    dialog = window.settings_dialog
    assert dialog.isVisible()
    assert dialog.port.value() == 7893
    dialog.port.setValue(7894)
    dialog.reject()
    assert window.store.export_settings()["proxy"]["port"] == 7893
    assert calls == ["reconnect"]


def test_settings_dialog_is_owned_visible_and_reused(window, qtbot):
    from tg_chat_client.models import ProxyConfig

    window.mock = False
    window.store.save_connection(123, "test-private-hash", ProxyConfig(port=7892))
    qtbot.mouseClick(window.settings_button, Qt.MouseButton.LeftButton)
    dialog = window.settings_dialog
    assert dialog.isVisible()
    assert window.geometry().intersects(dialog.geometry())
    window.open_connection_settings()
    assert window.settings_dialog is dialog
    dialog.reject()


def test_settings_button_tab_order_and_keyboard_activation(window, qtbot, monkeypatch):
    from PySide6.QtWidgets import QPushButton

    from tg_chat_client.models import ProxyConfig

    window.mock = False
    window.store.save_connection(123, "test-only-hash", ProxyConfig(port=7892))
    window.activateWindow()
    window.settings_button.setFocus()
    qtbot.waitUntil(window.settings_button.hasFocus)
    assert window.settings_button.isEnabled()
    assert (
        QApplication.widgetAt(
            window.settings_button.mapToGlobal(window.settings_button.rect().center())
        )
        is window.settings_button
    )
    qtbot.keyClick(window.settings_button, Qt.Key.Key_Tab)
    next_button = QApplication.focusWidget()
    assert isinstance(next_button, QPushButton)
    assert next_button.text() == "添加 / 重新登录"
    qtbot.keyClick(next_button, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
    assert window.settings_button.hasFocus()
    qtbot.keyClick(window.settings_button, Qt.Key.Key_Space)
    assert window.settings_dialog.isVisible()
    window.settings_dialog.reject()
    window.input.setFocus()
    qtbot.keyClick(window.input, Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
    assert window.settings_dialog.isVisible()
    window.settings_dialog.reject()


@pytest.mark.parametrize("blocked", ["mock", "send", "account", "login"])
def test_connection_settings_explains_why_it_cannot_open(window, qtbot, monkeypatch, blocked):
    from PySide6.QtWidgets import QDialog, QMessageBox, QPushButton

    from tg_chat_client.models import ProxyConfig

    window.store.save_connection(123, "test-only-hash", ProxyConfig(port=7892))
    before = window.store.export_settings()
    window.mock = blocked == "mock"
    if blocked == "send":
        window.pending_sends[window.target] = None
    elif blocked == "account":
        window.account_actions.add(window.account_id)
    elif blocked == "login":
        window.login_dialog = QDialog(window)
        window.login_dialog.show()
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args[2]))
    button = next(b for b in window.findChildren(QPushButton) if b.text() == "连接设置")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert len(messages) == 1
    assert messages[0] == window.notice.text()
    assert window.store.export_settings() == before
    window.pending_sends.clear()
    window.account_actions.clear()
    if window.login_dialog:
        window.login_dialog.close()


def test_logout_preserves_draft_and_disables_send(window, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    aid, chat = window.target
    window.input.setPlainText("保留的草稿")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.manage_account(aid, "logout")
    qtbot.waitUntil(lambda: not window.account_actions)
    assert window.store.draft(aid, chat)["text"] == "保留的草稿"
    assert window.statuses[aid] == "需要登录"
    assert window.accounts.count() == 5
    assert not window.send_button.isEnabled()
    window.send_text("不能发送", "input")
    assert not window.pending_sends


def test_remove_current_and_last_account_clears_view(window, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    while window.store.accounts():
        aid, chat = window.target
        window.input.setPlainText("需要删除的草稿")
        window.manage_account(aid, "remove_account")
        qtbot.waitUntil(lambda: not window.account_actions)
        assert not window.store.draft(aid, chat)["text"]
        assert all(t[0] != aid for t in window.buffers)
    assert window.account_id is None
    assert window.chat_id is None
    assert window.accounts.count() == 0
    assert window.messages.model().rowCount() == 0
    assert window.input.toPlainText() == ""
    assert not window.send_button.isEnabled()


def test_cancel_remove_keeps_account(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    window.manage_account(window.account_id, "remove_account")
    assert window.accounts.count() == 5
    assert not window.account_actions


def test_remove_other_account_preserves_current_draft(window, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    current = window.target
    other = window.store.accounts()[1]["id"]
    window.input.setPlainText("当前账号草稿")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.manage_account(other, "remove_account")
    qtbot.waitUntil(lambda: not window.account_actions)
    assert window.target == current
    assert window.input.toPlainText() == "当前账号草稿"
    assert window.accounts.count() == 4


def test_failed_remove_resets_controls_and_preserves_view(window):
    aid, chat = window.target
    window.input.setPlainText("失败时保留")
    window.account_actions.add(aid)
    window.handle(
        {"kind": "error", "operation": "remove_account", "account_id": aid, "message": "退出失败"}
    )
    assert not window.account_actions
    assert window.target == (aid, chat)
    assert window.input.toPlainText() == "失败时保留"
    assert window.accounts.count() == 5
    assert window.notice.text() == "退出失败"


def test_startup_with_saved_account_initializes_message_model(qtbot, tmp_path):
    store = Store(tmp_path)
    store.add_account("saved-account", "已保存账号", "")
    bridge = ServiceBridge(store, mock=True)
    window = MainWindow(store, bridge, mock=True)
    qtbot.addWidget(window)
    try:
        window.show()
        assert window.account_id == "saved-account"
        assert window.messages.model().rowCount() == 0
    finally:
        qtbot.waitUntil(lambda: window.statuses.get("saved-account") == "模拟连接")
        window.request_exit()


def test_closed_window_ignores_late_group_result(window, qtbot, monkeypatch):
    from PySide6.QtCore import QTimer

    aid = window.account_id
    request_id = window.group_requests[aid]
    window.chat_id = None
    calls = []
    original_submit = window.bridge.submit

    def submit(operation, **args):
        calls.append(operation)
        return original_submit(operation, **args)

    monkeypatch.setattr(window.bridge, "submit", submit)
    event = {
        "kind": "result",
        "operation": "groups",
        "account_id": aid,
        "request_id": request_id,
        "result": [{"id": -999, "title": "迟到的群列表"}],
    }
    # Model an already queued delivery: disconnecting the signal alone is insufficient.
    QTimer.singleShot(0, lambda: window.handle(event))
    window.request_exit()
    qtbot.wait(20)
    assert calls == []
    assert window.chat_id is None


def test_close_before_deferred_start_does_not_submit(qtbot, tmp_path, monkeypatch):
    store = Store(tmp_path)
    bridge = ServiceBridge(store, mock=True)
    window = MainWindow(store, bridge, mock=True)
    qtbot.addWidget(window)
    calls = []
    original_submit = bridge.submit

    def submit(operation, **args):
        calls.append(operation)
        return original_submit(operation, **args)

    monkeypatch.setattr(bridge, "submit", submit)
    window.request_exit()
    qtbot.wait(20)
    assert calls == []
    assert bridge.closed


def test_switch_restores_draft_and_quote(window, qtbot):
    first = window.account_id
    window.input.setPlainText("账号一草稿")
    window.quote(window.messages.model().index(0))
    reply_id = window.reply_id
    window.accounts.setCurrentRow(1)
    qtbot.waitUntil(lambda: window.chat_id is not None)
    window.input.setPlainText("账号二草稿")
    window.accounts.setCurrentRow(0)
    qtbot.waitUntil(lambda: window.account_id == first and window.chat_id is not None)
    assert window.input.toPlainText() == "账号一草稿"
    assert window.reply_id == reply_id


def test_send_during_switch_preserves_new_edits_and_other_account_draft(window, qtbot):
    first = window.account_id
    chat = window.chat_id
    window.input.clear()
    window.insert_phrase("快捷短句")
    window.quote(window.messages.model().index(0))
    window.send_text(window.input.toPlainText(), "input")
    window.input.setPlainText("保留的自由输入")
    window.accounts.setCurrentRow(1)
    qtbot.waitUntil(lambda: window.chat_id is not None)
    window.input.setPlainText("另一个账号")
    qtbot.waitUntil(lambda: not window.pending_sends)
    assert window.store.count(first, chat) == 1
    assert window.store.draft(first, chat)["text"] == "保留的自由输入"
    assert window.store.draft(first, chat)["reply_id"] is None
    assert window.input.toPlainText() == "另一个账号"


def test_ime_and_auto_repeat_do_not_send(qtbot):
    composer = Composer()
    qtbot.addWidget(composer)
    calls = []
    composer.send_requested.connect(lambda: calls.append(True))
    composer.inputMethodEvent(QInputMethodEvent("拼音", []))
    composer.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
    )
    assert not calls
    composer.inputMethodEvent(QInputMethodEvent("", []))
    composer.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier, "", True)
    )
    assert not calls
    composer.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
    )
    assert len(calls) == 1


def test_enter_sends_once_and_shift_enter_adds_newline(window, qtbot):
    window.input.setPlainText("测试发送")
    window.input.setFocus()
    qtbot.keyClick(window.input, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: not window.pending_sends)
    assert window.store.count(window.account_id, window.chat_id) == 1
    assert window.input.toPlainText() == ""
    qtbot.keyClick(window.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert "\n" in window.input.toPlainText()


def test_submit_clears_immediately_and_success_preserves_identical_next_draft(
    window, qtbot, monkeypatch
):
    calls = []
    monkeypatch.setattr(window.bridge, "submit", lambda operation, **args: calls.append(args))
    window.quote(window.messages.model().index(0))
    reply_id = window.reply_id
    window.input.setPlainText("重复短句")
    qtbot.keyClick(window.input, Qt.Key.Key_Return)
    request = calls[0]["request"]
    assert request.reply_to == reply_id
    assert window.input.toPlainText() == ""
    assert window.store.draft(*window.target)["text"] == ""
    assert window.reply_id is None

    window.input.setPlainText("重复短句")
    window.quote(window.messages.model().index(0))
    qtbot.keyClick(window.input, Qt.Key.Key_Return)
    assert len(calls) == 1
    assert "上一条仍在发送" in window.notice.text()
    message = replace(
        next(iter(window.buffers[window.target].values())),
        message_id=99999,
        text=request.text,
    )
    window.handle({"kind": "result", "operation": "send", "request": request, "result": message})
    assert window.input.toPlainText() == "重复短句"
    assert window.reply_id == reply_id
    qtbot.keyClick(window.input, Qt.Key.Key_Return)
    assert len(calls) == 2
    assert calls[1]["request"].text == "重复短句"
    assert window.input.toPlainText() == ""
    window.handle({"kind": "result", "operation": "send", "request": request, "result": message})
    assert window.pending_sends[window.target][0] == calls[1]["request"]
    window.pending_sends.clear()


@pytest.mark.parametrize("next_draft", ["", "下一句"])
@pytest.mark.parametrize("switch", [False, True])
def test_send_failure_restores_text_without_losing_next_draft(
    window, qtbot, monkeypatch, next_draft, switch
):
    calls = []
    original_submit = window.bridge.submit

    def submit(operation, **args):
        if operation == "send":
            calls.append(args["request"])
        else:
            return original_submit(operation, **args)

    monkeypatch.setattr(window.bridge, "submit", submit)
    target = window.target
    window.quote(window.messages.model().index(0))
    reply_id, reply_text = window.reply_id, window.reply_text
    window.input.setPlainText("未成功文本")
    qtbot.keyClick(window.input, Qt.Key.Key_Return)
    window.input.setPlainText(next_draft)
    if switch:
        window.accounts.setCurrentRow(1)
        qtbot.waitUntil(lambda: window.chat_id is not None)
        window.input.setPlainText("其他账号草稿")
    event = {
        "kind": "error",
        "operation": "send",
        "request": calls[0],
        "message": "发送结果不确定，请先查看群消息，不要直接重复发送",
    }
    window.handle(event)
    expected = "未成功文本" + ("\n" + next_draft if next_draft else "")
    draft = window.store.draft(*target)
    assert draft["text"] == expected
    assert draft["reply_id"] == (None if next_draft else reply_id)
    assert draft["reply_text"] == ("" if next_draft else reply_text)
    assert window.input.toPlainText() == ("其他账号草稿" if switch else expected)
    assert "不确定" in window.notice.text()
    assert "原文本已恢复" in window.notice.text()
    window.handle(event)
    assert window.store.draft(*target)["text"] == expected
    assert len(calls) == 1


@pytest.mark.parametrize("browse_history", [False, True])
def test_successful_send_returns_to_bottom(window, qtbot, browse_history):
    window.go_bottom()
    bar = window.messages.verticalScrollBar()
    if browse_history:
        bar.setValue(0)
        assert bar.value() < bar.maximum()
    window.input.setPlainText("发送后应看到这条消息")
    qtbot.keyClick(window.input, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: window.store.count(*window.target) == 1 and not window.pending_sends)
    qtbot.waitUntil(lambda: bar.value() == bar.maximum())
    assert window.bottom.text() == "回到底部"
    last = window.messages.model().index(window.messages.model().rowCount() - 1)
    assert window.messages.visualRect(last).intersects(window.messages.viewport().rect())


def test_incoming_message_preserves_history_scroll(window, qtbot):
    bar = window.messages.verticalScrollBar()
    bar.setValue(0)
    messages = window.buffers[window.target]
    message = replace(
        messages[max(messages)], message_id=max(messages) + 1, text="收到的新消息", outgoing=False
    )
    window.handle({"kind": "message", "message": message})
    qtbot.wait(20)
    assert bar.value() == 0
    assert bar.value() < bar.maximum()
    assert window.bottom.text() == "有新消息 · 回到底部"


def test_other_account_send_result_preserves_scroll(window, qtbot):
    original = window.target
    messages = window.buffers[original]
    message = replace(messages[max(messages)], message_id=max(messages) + 1, outgoing=True)
    request = SendRequest("other-account-send", *original, message.text)
    window.accounts.setCurrentRow(1)
    qtbot.waitUntil(
        lambda: (
            window.chat_id is not None
            and window.messages.model().rowCount() >= 30
            and window.target not in window.history_requests
        )
    )
    bar = window.messages.verticalScrollBar()
    bar.setValue(0)
    window.handle({"kind": "result", "operation": "send", "request": request, "result": message})
    assert bar.value() == 0


def test_media_is_placeholder_and_never_creates_preview(window, qtbot):
    index = window.messages.model().index(4)
    window.messages.scrollTo(index)
    qtbot.wait(20)
    assert "[图片]" in index.data()
    assert "加载中" not in index.data()
    assert list((window.store.root / "cache").iterdir()) == []


def test_shortcuts_do_not_fire_in_dialog(window, qtbot):
    from PySide6.QtWidgets import QDialog

    dialog = QDialog(window)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is dialog)
    original_account = window.account_id
    window.input.setPlainText("保留草稿")
    qtbot.keyClick(dialog, Qt.Key.Key_2, Qt.KeyboardModifier.AltModifier)
    qtbot.keyClick(dialog, Qt.Key.Key_1, Qt.KeyboardModifier.ControlModifier)
    assert window.account_id == original_account
    assert window.input.toPlainText() == "保留草稿"
    assert not window.pending_sends


@pytest.mark.parametrize(
    "draft,start,end,expected",
    [("", 0, 0, "短句"), ("前后", 1, 1, "前短句后"), ("前替换后", 1, 3, "前短句后")],
)
def test_phrase_inserts_at_cursor_without_sending(window, qtbot, draft, start, end, expected):
    window.store.replace_phrases(["短句"])
    window.refresh_phrases()
    window.input.setPlainText(draft)
    window.quote(window.messages.model().index(0))
    reply_id = window.reply_id
    cursor = window.input.textCursor()
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    window.input.setTextCursor(cursor)
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window)
    qtbot.mouseClick(window.phrase_layout.itemAt(0).widget(), Qt.MouseButton.LeftButton)
    assert window.input.toPlainText() == expected
    assert window.input.textCursor().position() == start + len("短句")
    assert window.input.hasFocus()
    assert window.reply_id == reply_id
    assert window.store.draft(*window.target)["text"] == expected
    assert not window.pending_sends
    assert window.store.count(*window.target) == 0
    qtbot.keyClick(window.input, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: window.store.count(*window.target) == 1 and not window.pending_sends)
    assert window.input.toPlainText() == ""


def test_alt_switches_accounts_and_preserves_inserted_draft(window, qtbot):
    first = window.account_id
    window.input.setPlainText("草稿")
    window.input.moveCursor(QTextCursor.MoveOperation.End)
    window.insert_phrase("短句")
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window)
    qtbot.keyClick(window.input, Qt.Key.Key_2, Qt.KeyboardModifier.AltModifier)
    qtbot.waitUntil(lambda: window.account_id != first and window.chat_id is not None)
    window.input.setPlainText("另一账号")
    qtbot.keyClick(window.input, Qt.Key.Key_1, Qt.KeyboardModifier.AltModifier)
    qtbot.waitUntil(lambda: window.account_id == first and window.chat_id is not None)
    assert window.input.toPlainText() == "草稿短句"
    assert not window.pending_sends


def test_removed_phrase_shortcut_and_account_ime_guard(window, qtbot):
    window.store.replace_phrases(["短句"])
    window.refresh_phrases()
    window.input.setPlainText("草稿")
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window)
    QApplication.sendEvent(
        window.input,
        QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_1, Qt.KeyboardModifier.ControlModifier, "", True
        ),
    )
    assert window.input.toPlainText() == "草稿"
    window.input.inputMethodEvent(QInputMethodEvent("拼音", []))
    qtbot.keyClick(window.input, Qt.Key.Key_1, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClick(window.input, Qt.Key.Key_2, Qt.KeyboardModifier.AltModifier)
    assert window.accounts.currentRow() == 0
    assert window.input.toPlainText() == "草稿"
    window.input.inputMethodEvent(QInputMethodEvent("", []))


def test_tenth_phrase_can_be_clicked_and_panel_is_on_right(window, qtbot):
    from PySide6.QtWidgets import QSplitter

    window.store.replace_phrases([f"短句{i}" for i in range(1, 11)])
    window.refresh_phrases()
    splitter = window.findChild(QSplitter)
    assert splitter.count() == 3
    assert splitter.widget(2).isAncestorOf(window.phrase_widget)
    button = window.phrase_layout.itemAt(9).widget()
    assert "Ctrl+" not in button.text()
    window.input.clear()
    button.click()
    assert window.input.toPlainText() == "短句10"
    assert not window.pending_sends


def test_ninth_account_shortcut_and_current_phrase_order(window, qtbot):
    for i in range(6, 10):
        window.store.add_account(f"extra-{i}", f"模拟账号{i}", "")
    window.refresh_accounts()
    window.store.replace_phrases([f"短句{i}" for i in range(9, 0, -1)])
    window.refresh_phrases()
    window.input.clear()
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window)
    qtbot.keyClick(window.input, Qt.Key.Key_9, Qt.KeyboardModifier.ControlModifier)
    assert window.input.toPlainText() == ""
    window.phrase_layout.itemAt(8).widget().click()
    assert window.input.toPlainText() == "短句1"
    qtbot.keyClick(window.input, Qt.Key.Key_9, Qt.KeyboardModifier.AltModifier)
    assert window.accounts.currentRow() == 8
    assert window.account_id == "extra-9"
    assert not window.pending_sends


def test_phrase_insertion_during_send_preserves_new_draft(window, qtbot):
    window.input.setPlainText("提交内容")
    window.send_text(window.input.toPlainText(), "input")
    assert window.pending_sends
    assert window.phrase_widget.isEnabled()
    window.input.moveCursor(QTextCursor.MoveOperation.End)
    window.insert_phrase("后续草稿")
    qtbot.waitUntil(lambda: not window.pending_sends)
    assert window.input.toPlainText() == "后续草稿"
    assert window.store.count(*window.target) == 1


@pytest.mark.parametrize(
    "prefix,draft,position,expected",
    [
        (";", ";n", 2, "你好"),
        (";", "；n", 2, "你好"),
        ("；", ";N", 2, "你好"),
        (";+", "前；+n后", 4, "前你好后"),
        (";", "😀；n", 4, "😀你好"),
        (";", "；unknow", 7, "；unknowh"),
        (";", "前;N后", 3, "前你好后"),
        (";+", "前;+n后", 4, "前你好后"),
        (";", "😀;n", 4, "😀你好"),
        (";", ";nm", 3, ";nmh"),
        (";", ";unknow", 7, ";unknowh"),
        (";", ";nm", 2, ";nhm"),
    ],
)
def test_alias_expansion_preserves_draft_and_quote(
    window, qtbot, prefix, draft, position, expected
):
    window.store.replace_phrases(
        [{"text": "你好", "alias": "nh"}, {"text": "其他缩写", "alias": "nm"}], prefix=prefix
    )
    window.refresh_phrases()
    window.input.setPlainText(draft)
    window.quote(window.messages.model().index(0))
    reply = window.reply_id
    cursor = window.input.textCursor()
    cursor.setPosition(position)
    window.input.setTextCursor(cursor)
    qtbot.keyClicks(window.input, "h")
    assert window.input.toPlainText() == expected
    assert window.reply_id == reply
    assert window.store.draft(*window.target)["text"] == expected
    assert not window.pending_sends
    assert window.store.count(*window.target) == 0


@pytest.mark.parametrize("prefix", [";", "；"])
def test_expansion_undo_and_manual_send(window, qtbot, prefix):
    window.store.replace_phrases([{"text": "你好", "alias": "nh"}])
    window.refresh_phrases()
    window.input.setPlainText(f"前{prefix}n后")
    cursor = window.input.textCursor()
    cursor.setPosition(3)
    window.input.setTextCursor(cursor)
    qtbot.keyClicks(window.input, "h")
    assert window.input.toPlainText() == "前你好后"
    assert window.input.textCursor().position() == 3
    window.input.undo()
    assert window.input.toPlainText() == f"前{prefix}nh后"
    window.input.redo()
    qtbot.keyClick(window.input, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: window.store.count(*window.target) == 1 and not window.pending_sends)
    assert window.input.toPlainText() == ""


def test_expansion_does_not_run_for_ime_selection_or_loading(qtbot):
    composer = Composer()
    qtbot.addWidget(composer)
    composer.phrase_aliases = {"nh": "你好"}
    composer.setPlainText(";n")
    assert composer.toPlainText() == ";n"
    composer.moveCursor(QTextCursor.MoveOperation.End)
    composer.inputMethodEvent(QInputMethodEvent("拼音", []))
    qtbot.keyClicks(composer, "h")
    assert "你好" not in composer.toPlainText()
    composer.inputMethodEvent(QInputMethodEvent("", []))
    composer.setPlainText(";n")
    composer.selectAll()
    qtbot.keyClicks(composer, "h")
    assert composer.toPlainText() == "h"
    composer.setPlainText(";n")
    composer.moveCursor(QTextCursor.MoveOperation.End)
    QApplication.sendEvent(
        composer,
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_H, Qt.KeyboardModifier.NoModifier, "h", True),
    )
    assert composer.toPlainText() == ";nh"


@pytest.mark.parametrize("prefix,commit", [(";", "nh"), ("；", "NH"), ("", "；nh")])
def test_ime_committed_alias_expands_and_preserves_quote(window, qtbot, prefix, commit):
    window.store.replace_phrases([{"text": "你好", "alias": "nh"}])
    window.refresh_phrases()
    window.input.setPlainText(f"😀前{prefix}后")
    cursor = window.input.textCursor()
    cursor.setPosition(len(f"😀前{prefix}".encode("utf-16-le")) // 2)
    window.input.setTextCursor(cursor)
    window.quote(window.messages.model().index(0))
    reply = window.reply_id
    window.input.inputMethodEvent(QInputMethodEvent("n", []))
    event = QInputMethodEvent("", [])
    event.setCommitString(commit)
    window.input.inputMethodEvent(event)
    assert window.input.toPlainText() == "😀前你好后"
    assert window.reply_id == reply
    assert window.store.draft(*window.target)["text"] == "😀前你好后"
    assert not window.pending_sends
    window.input.undo()
    assert window.input.toPlainText() == f"😀前{prefix}{commit}后"


@pytest.mark.parametrize("prefix,preedit", [(";", "nh"), ("；", "NH"), (";+", "n h"), (";", "n'h")])
def test_ime_candidate_alias_expands_immediately(window, qtbot, prefix, preedit):
    window.store.replace_phrases([{"text": "你好", "alias": "nh"}], prefix=prefix)
    window.refresh_phrases()
    window.input.setPlainText(f"😀前{prefix}后")
    cursor = window.input.textCursor()
    cursor.setPosition(len(f"😀前{prefix}".encode("utf-16-le")) // 2)
    window.input.setTextCursor(cursor)
    window.quote(window.messages.model().index(0))
    reply = window.reply_id
    window.input.inputMethodEvent(QInputMethodEvent("n", []))
    assert window.input.composing
    assert window.input.toPlainText() == f"😀前{prefix}后"
    window.input.inputMethodEvent(QInputMethodEvent(preedit, []))
    assert window.input.toPlainText() == "😀前你好后"
    assert not window.input.composing
    assert window.reply_id == reply
    assert window.store.draft(*window.target)["text"] == "😀前你好后"
    assert not window.pending_sends
    window.input.inputMethodEvent(QInputMethodEvent("", []))
    assert window.input.toPlainText() == "😀前你好后"
    window.input.undo()
    alias = preedit.replace(" ", "").replace("'", "")
    assert window.input.toPlainText() == f"😀前{prefix}{alias}后"


@pytest.mark.parametrize("draft,preedit", [("", "nh"), (";", "nihao"), (";", "你好"), (";", "nx")])
def test_unmatched_ime_candidates_continue_normally(qtbot, draft, preedit):
    composer = Composer()
    qtbot.addWidget(composer)
    composer.phrase_aliases = {"nh": "你好"}
    composer.setPlainText(draft)
    composer.moveCursor(QTextCursor.MoveOperation.End)
    composer.inputMethodEvent(QInputMethodEvent(preedit, []))
    assert composer.toPlainText() == draft
    assert composer.composing


def test_ime_reset_does_not_commit_duplicate_candidate(qtbot, monkeypatch):
    composer = Composer()
    qtbot.addWidget(composer)
    composer.show()
    composer.setFocus()
    qtbot.waitUntil(composer.hasFocus)
    composer.phrase_aliases = {"nh": "你好"}
    composer.setPlainText(";")
    composer.moveCursor(QTextCursor.MoveOperation.End)
    resets = []

    class InputMethod:
        def reset(self):
            resets.append(True)
            event = QInputMethodEvent("", [])
            event.setCommitString("你好")
            composer.inputMethodEvent(event)

    monkeypatch.setattr(QApplication, "inputMethod", lambda: InputMethod())
    composer.inputMethodEvent(QInputMethodEvent("nh", []))
    assert resets == []
    qtbot.waitUntil(lambda: not composer.resetting_input_method)
    assert resets == [True]
    assert composer.toPlainText() == "你好"
    assert not composer.composing


def test_late_candidate_commit_is_discarded_before_deferred_cancel(qtbot, monkeypatch):
    from tg_chat_client import ui

    composer = Composer()
    qtbot.addWidget(composer)
    composer.show()
    composer.setFocus()
    qtbot.waitUntil(composer.hasFocus)
    cancelled = []
    monkeypatch.setattr(ui, "cancel_windows_composition", lambda hwnd: cancelled.append(hwnd))
    composer.phrase_aliases = {"nh": "短句正文"}
    composer.setPlainText(";")
    composer.moveCursor(QTextCursor.MoveOperation.End)
    composer.inputMethodEvent(QInputMethodEvent("nh", []))
    # A native input method can deliver a commit after the expansion callback returns.
    stale_commit = QInputMethodEvent("", [])
    stale_commit.setCommitString("你好")
    composer.inputMethodEvent(stale_commit)
    assert composer.toPlainText() == "短句正文"
    qtbot.waitUntil(lambda: not composer.resetting_input_method)
    assert len(cancelled) == 1
    qtbot.keyClick(composer, Qt.Key.Key_Space)
    assert composer.toPlainText() == "短句正文 "
    composer.inputMethodEvent(QInputMethodEvent("zhongwen", []))
    fresh_commit = QInputMethodEvent("", [])
    fresh_commit.setCommitString("中文")
    composer.inputMethodEvent(fresh_commit)
    assert composer.toPlainText() == "短句正文 中文"


def test_candidate_focus_session_is_closed_and_restored(window, qtbot, monkeypatch):
    from tg_chat_client import ui

    monkeypatch.setattr(ui, "cancel_windows_composition", lambda hwnd: None)
    composer = window.input
    composer.phrase_aliases = {"nh": "短句正文"}
    composer.setPlainText(";")
    composer.moveCursor(QTextCursor.MoveOperation.End)
    composer.setFocus()
    qtbot.waitUntil(composer.hasFocus)
    lost_focus = []

    def focus_changed(previous, current):
        if previous is composer:
            lost_focus.append(True)
            # Some IMEs flush the old candidate when the editable focus is removed.
            event = QInputMethodEvent("", [])
            event.setCommitString("你好")
            composer.inputMethodEvent(event)

    QApplication.instance().focusChanged.connect(focus_changed)
    try:
        composer.inputMethodEvent(QInputMethodEvent("nh", []))
        qtbot.waitUntil(lambda: not composer.resetting_input_method)
        assert lost_focus
        assert composer.hasFocus()
        assert composer.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
        assert composer.toPlainText() == "短句正文"
        qtbot.keyClick(composer, Qt.Key.Key_Space)
        assert composer.toPlainText() == "短句正文 "
    finally:
        QApplication.instance().focusChanged.disconnect(focus_changed)


def test_ime_focus_restore_does_not_steal_focus_from_another_field(window, qtbot, monkeypatch):
    from tg_chat_client import ui

    monkeypatch.setattr(ui, "cancel_windows_composition", lambda hwnd: None)
    composer = window.input
    composer.phrase_aliases = {"nh": "短句正文"}
    composer.setPlainText(";")
    composer.moveCursor(QTextCursor.MoveOperation.End)
    composer.setFocus()
    qtbot.waitUntil(composer.hasFocus)
    composer.inputMethodEvent(QInputMethodEvent("nh", []))
    composer.ime_reset_timer.stop()
    composer.finish_ime_reset()
    assert not composer.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
    window.search.setFocus()
    qtbot.waitUntil(lambda: not composer.resetting_input_method)
    assert window.search.hasFocus()
    assert composer.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
    assert composer.toPlainText() == "短句正文"


def test_ime_prefix_commit_and_candidate_can_arrive_together(window, qtbot):
    window.store.replace_phrases([{"text": ";dj", "alias": "nh"}, {"text": "你好", "alias": "dj"}])
    window.refresh_phrases()
    window.input.clear()
    event = QInputMethodEvent("nh", [])
    event.setCommitString("；")
    window.input.inputMethodEvent(event)
    assert window.input.toPlainText() == ";dj"
    assert not window.pending_sends
    qtbot.waitUntil(lambda: not window.input.resetting_input_method)
    window.input.inputMethodEvent(QInputMethodEvent("zhongwen", []))
    event = QInputMethodEvent("", [])
    event.setCommitString("中文")
    window.input.inputMethodEvent(event)
    assert window.input.toPlainText() == ";dj中文"
    assert window.store.draft(*window.target)["text"] == ";dj中文"
    qtbot.keyClick(window.input, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: window.store.count(*window.target) == 1 and not window.pending_sends)


def test_pasted_alias_and_expanded_content_do_not_expand_recursively(qtbot):
    composer = Composer()
    qtbot.addWidget(composer)
    composer.phrase_aliases = {"nh": ";dj", "dj": "你好"}
    QApplication.clipboard().setText(";nh ")
    composer.paste()
    assert composer.toPlainText() == ";nh "
    composer.setPlainText(";n")
    composer.moveCursor(QTextCursor.MoveOperation.End)
    qtbot.keyClicks(composer, "h")
    assert composer.toPlainText() == ";dj"


def test_phrase_dialog_saves_alias_order_and_prefix(window, qtbot, monkeypatch):
    from PySide6.QtWidgets import QDialog, QMessageBox

    from tg_chat_client.ui import PhraseDialog

    dialog = PhraseDialog(window.store, window)
    qtbot.addWidget(dialog)
    dialog.list.clear()
    dialog.add_item({"text": "你好", "alias": "NH"})
    dialog.add_item({"text": "共享", "alias": "nh"})
    errors = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: errors.append(args[2]))
    dialog.prefix.setText(";")
    dialog.save()
    assert errors and dialog.result() != QDialog.DialogCode.Accepted
    dialog.list.takeItem(1)
    dialog.add_item({"text": "点我", "alias": ""})
    item = dialog.list.takeItem(1)
    dialog.list.insertItem(0, item)
    dialog.prefix.setText(";+")
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    window.refresh_phrases()
    assert [p["text"] for p in window.phrases] == ["点我", "你好"]
    assert window.input.phrase_prefix == ";+"
    assert ";+nh" in window.phrase_layout.itemAt(1).widget().text()


def test_complete_trigger_replaces_immediately_and_space_is_literal(window, qtbot):
    window.store.replace_phrases([{"text": "短句", "alias": "wz"}], prefix=";+")
    window.refresh_phrases()
    window.input.clear()
    qtbot.keyClicks(window.input, ";+w")
    assert window.input.toPlainText() == ";+w"
    qtbot.keyClicks(window.input, "z")
    assert window.input.toPlainText() == "短句"
    assert not window.pending_sends
    qtbot.keyClick(window.input, Qt.Key.Key_Space)
    assert window.input.toPlainText() == "短句 "


def test_legacy_prefix_conflicts_only_disable_conflicting_aliases(window, qtbot):
    with window.store.connect() as db:
        db.execute("DELETE FROM phrases")
        db.executemany(
            "INSERT INTO phrases(text,position,alias) VALUES(?,?,?)",
            [("短", 0, "wz"), ("长", 1, "wzd"), ("你好", 2, "nh")],
        )
    window.refresh_phrases()
    assert window.input.phrase_aliases == {"nh": "你好"}
    assert "wz、wzd" in window.phrase_layout.itemAt(0).widget().text()
    window.input.clear()
    qtbot.keyClicks(window.input, ";wzd")
    assert window.input.toPlainText() == ";wzd"
    qtbot.mouseClick(window.phrase_layout.itemAt(1).widget(), Qt.MouseButton.LeftButton)
    assert window.input.toPlainText() == ";wzd短"


def test_phrase_editor_rejects_prefix_conflict_before_adding(window, qtbot, monkeypatch):
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QLineEdit, QTextEdit

    from tg_chat_client.ui import PhraseDialog

    dialog = PhraseDialog(window.store, window)
    qtbot.addWidget(dialog)
    dialog.list.clear()
    dialog.add_item({"text": "原短句", "alias": "wz"})

    def edit_form(editor):
        alias = editor.findChild(QLineEdit)
        alias.setText("WZD")
        editor.findChild(QTextEdit).setPlainText("新短句")
        editor.findChild(QDialogButtonBox).accepted.emit()
        assert editor.result() != QDialog.DialogCode.Accepted
        assert any("缩写冲突" in label.text() for label in editor.findChildren(QLabel))
        alias.setText("wd")
        editor.findChild(QDialogButtonBox).accepted.emit()
        return editor.result()

    monkeypatch.setattr(QDialog, "exec", edit_form)
    dialog.add()
    assert dialog.list.count() == 2
    assert dialog.list.item(1).data(Qt.ItemDataRole.UserRole)["alias"] == "wd"

    def edit_existing(editor):
        editor.findChild(QLineEdit).setText("WZ")
        editor.findChild(QDialogButtonBox).accepted.emit()
        return editor.result()

    monkeypatch.setattr(QDialog, "exec", edit_existing)
    dialog.list.setCurrentRow(0)
    dialog.edit()
    assert dialog.list.item(0).data(Qt.ItemDataRole.UserRole)["alias"] == "wz"


def test_expansion_during_send_and_switch_preserves_new_draft(window, qtbot):
    window.store.replace_phrases([{"text": "新增草稿", "alias": "nh"}])
    window.refresh_phrases()
    first = window.target
    window.input.setPlainText("原提交")
    window.send_text(window.input.toPlainText(), "input")
    assert window.pending_sends
    window.input.setPlainText(";n")
    window.input.moveCursor(QTextCursor.MoveOperation.End)
    qtbot.keyClicks(window.input, "h")
    assert window.input.toPlainText() == "新增草稿"
    window.accounts.setCurrentRow(1)
    qtbot.waitUntil(lambda: window.chat_id is not None)
    window.input.setPlainText(";N")
    window.input.moveCursor(QTextCursor.MoveOperation.End)
    qtbot.keyClicks(window.input, "H")
    assert window.input.toPlainText() == "新增草稿"
    qtbot.waitUntil(lambda: not window.pending_sends)
    assert window.store.count(*first) == 1
    assert window.store.count(*window.target) == 0
    assert window.store.draft(*first)["text"] == "新增草稿"
    window.accounts.setCurrentRow(0)
    qtbot.waitUntil(lambda: window.target == first)
    assert window.input.toPlainText() == "新增草稿"


def test_message_bubbles_align_and_quote_from_bubble_and_blank_space(window, qtbot):
    from PySide6.QtCore import QPoint

    from tg_chat_client.models import ChatMessage

    aid, chat = window.target
    for mid, outgoing in ((9001, False), (9002, True)):
        window.put_message(
            ChatMessage(
                aid, chat, mid, "<b>同名</b>", "短句", "12:00", outgoing=outgoing, sender_id=42
            )
        )
    window.render_messages()
    qtbot.wait(50)
    from tg_chat_client.message_view import MESSAGE_ROLE

    incoming = window.messages.model().index_for(9001)
    outgoing = window.messages.model().index_for(9002)
    delegate = window.messages.itemDelegate()
    incoming_doc, _ = delegate.layout(incoming.data(MESSAGE_ROLE), window.messages.font())
    outgoing_doc, size = delegate.layout(outgoing.data(MESSAGE_ROLE), window.messages.font())
    assert "<b>同名</b>" in incoming_doc.toPlainText()
    assert "<b>同名</b>（我）" in outgoing_doc.toPlainText()
    assert delegate.bubble_rect(incoming).left() == 8
    assert size.width() < window.messages.viewport().width() * 0.75
    item = outgoing
    window.messages.scrollTo(item)
    qtbot.wait(30)
    rect = window.messages.visualRect(item)
    assert delegate.bubble_rect(item).right() >= window.messages.viewport().width() - 10
    bubble_point = delegate.bubble_rect(item).center().toPoint()
    qtbot.mouseClick(window.messages.viewport(), Qt.MouseButton.LeftButton, pos=bubble_point)
    qtbot.mouseDClick(window.messages.viewport(), Qt.MouseButton.LeftButton, pos=bubble_point)
    assert window.reply_id == 9002
    window.cancel_quote()
    qtbot.mouseClick(
        window.messages.viewport(),
        Qt.MouseButton.LeftButton,
        pos=QPoint(rect.left() + 4, rect.center().y()),
    )
    qtbot.mouseDClick(
        window.messages.viewport(),
        Qt.MouseButton.LeftButton,
        pos=QPoint(rect.left() + 4, rect.center().y()),
    )
    assert window.reply_id == 9002


def test_message_context_menu_quotes_right_bubble(window, qtbot):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMenu

    from tg_chat_client.message_view import MESSAGE_ROLE

    model = window.messages.model()
    outgoing_item = next(
        model.index(row)
        for row in range(model.rowCount())
        if model.index(row).data(MESSAGE_ROLE).outgoing
    )
    window.messages.scrollTo(outgoing_item)
    qtbot.wait(30)
    pos = window.messages.itemDelegate().bubble_rect(outgoing_item).center().toPoint()

    def choose_reply():
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        menu.setActiveAction(menu.actions()[0])
        qtbot.keyClick(menu, Qt.Key.Key_Return)

    QTimer.singleShot(0, choose_reply)
    window.message_menu(pos)
    assert window.reply_id == outgoing_item.data(Qt.ItemDataRole.UserRole)


def test_message_resize_wraps_text_and_preserves_scroll(window, qtbot):
    from tg_chat_client.models import ChatMessage

    aid, chat = window.target
    message = ChatMessage(
        aid, chat, 9003, "长昵称" * 30, "长正文" * 200, "12:00", media="photo", sender_id=9
    )
    window.put_message(message)
    window.render_messages()
    qtbot.wait(50)
    delegate = window.messages.itemDelegate()
    _, wide = delegate.layout(message, window.messages.font())
    window.resize(850, 650)
    qtbot.wait(50)
    doc, narrow = delegate.layout(message, window.messages.font())
    assert narrow.width() < wide.width()
    assert narrow.width() <= (window.messages.viewport().width() - 16) * 0.75
    assert narrow.height() > wide.height()
    assert "[图片]" in doc.toPlainText()
    assert window.messages.horizontalScrollBar().maximum() == 0
    window.messages.verticalScrollBar().setValue(0)
    window.resize(1000, 700)
    qtbot.wait(50)
    assert window.messages.verticalScrollBar().value() == 0
    window.messages.scrollToBottom()
    window.resize(1100, 700)
    qtbot.wait(50)
    bar = window.messages.verticalScrollBar()
    assert bar.value() == bar.maximum()


def test_sender_color_is_stable_with_missing_identity():
    from tg_chat_client.message_view import sender_color

    assert sender_color(None) == "#666666"
    assert sender_color(42) == sender_color(42)
    assert sender_color(42) != sender_color(43)
