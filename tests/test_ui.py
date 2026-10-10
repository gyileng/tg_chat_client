import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QInputMethodEvent, QKeyEvent, QTextCursor
from PySide6.QtWidgets import QApplication

from tg_chat_client.bridge import ServiceBridge
from tg_chat_client.store import Store
from tg_chat_client.ui import Composer, MainWindow


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
@pytest.mark.parametrize("trigger", ["click", "shortcut"])
def test_phrase_inserts_at_cursor_without_sending(
    window, qtbot, draft, start, end, expected, trigger
):
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
    if trigger == "click":
        qtbot.mouseClick(window.phrase_layout.itemAt(0).widget(), Qt.MouseButton.LeftButton)
    else:
        window.input.setFocus()
        qtbot.keyClick(window.input, Qt.Key.Key_1, Qt.KeyboardModifier.ControlModifier)
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


def test_phrase_shortcut_ignores_repeat_and_ime(window, qtbot):
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
