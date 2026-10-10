from dataclasses import replace

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QInputMethodEvent, QKeyEvent, QTextCursor
from PySide6.QtWidgets import QApplication

from tg_chat_client.bridge import ServiceBridge
from tg_chat_client.models import SendRequest
from tg_chat_client.store import Store
from tg_chat_client.ui import Composer, MainWindow


def test_connection_settings_button_reopens_and_updates_proxy(window, qtbot, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QPushButton

    from tg_chat_client.models import ProxyConfig
    from tg_chat_client.ui import SettingsDialog

    window.store.save_connection(123, "test-only-hash", ProxyConfig(port=7892))
    window.mock = False
    calls = []
    monkeypatch.setattr(window.bridge, "submit", lambda op, **kw: calls.append(op))
    button = next(b for b in window.findChildren(QPushButton) if b.text() == "连接设置")
    opened = []

    def update_port():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, SettingsDialog)
        opened.append(dialog.port.value())
        dialog.port.setValue(7893)
        dialog.save()

    QTimer.singleShot(0, update_port)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert opened == [7892]
    assert window.store.export_settings()["proxy"]["port"] == 7893
    assert calls == ["reconnect"]

    def cancel():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, SettingsDialog)
        opened.append(dialog.port.value())
        dialog.port.setValue(7894)
        dialog.reject()

    QTimer.singleShot(0, cancel)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert opened == [7892, 7893]
    assert window.store.export_settings()["proxy"]["port"] == 7893
    assert calls == ["reconnect"]


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
    assert window.messages.count() == 0
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


@pytest.fixture
def window(qtbot, tmp_path):
    store = Store(tmp_path)
    bridge = ServiceBridge(store, mock=True)
    window = MainWindow(store, bridge, mock=True)
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    qtbot.waitUntil(lambda: window.accounts.count() == 5 and window.messages.count() >= 30)
    yield window
    window.close()


def test_startup_with_saved_account_initializes_preview_timer(qtbot, tmp_path):
    store = Store(tmp_path)
    store.add_account("saved-account", "已保存账号", "")
    bridge = ServiceBridge(store, mock=True)
    window = MainWindow(store, bridge, mock=True)
    qtbot.addWidget(window)
    try:
        assert window.account_id == "saved-account"
        assert window.preview_timer.isActive()
    finally:
        qtbot.waitUntil(lambda: window.statuses.get("saved-account") == "模拟连接")
        window.close()


def test_switch_restores_draft_and_quote(window, qtbot):
    first = window.account_id
    window.input.setPlainText("账号一草稿")
    window.quote(window.messages.item(0))
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
    window.quote(window.messages.item(0))
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
    last = window.messages.item(window.messages.count() - 1)
    assert window.messages.visualItemRect(last).intersects(window.messages.viewport().rect())


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
            and window.messages.count() >= 30
            and window.target not in window.history_requests
        )
    )
    bar = window.messages.verticalScrollBar()
    bar.setValue(0)
    window.handle({"kind": "result", "operation": "send", "request": request, "result": message})
    assert bar.value() == 0


def test_preview_fixture_is_displayable(window, qtbot):
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QAbstractItemView

    window.messages.scrollToItem(
        window.messages.item(4), QAbstractItemView.ScrollHint.PositionAtCenter
    )
    qtbot.waitUntil(lambda: bool(list((window.store.root / "cache").glob("*.png"))))
    assert not QPixmap(str(window.store.root / "cache" / "mock.png")).isNull()


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
    window.quote(window.messages.item(0))
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
    assert window.input.toPlainText() == "提交内容后续草稿"
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
    window.quote(window.messages.item(0))
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
