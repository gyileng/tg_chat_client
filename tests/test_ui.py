import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QInputMethodEvent, QKeyEvent
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


def test_phrase_send_during_switch_preserves_both_drafts(window, qtbot):
    first = window.account_id
    chat = window.chat_id
    window.input.setPlainText("保留的自由输入")
    window.quote(window.messages.item(0))
    window.send_text("快捷短句", "phrase")
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
    qtbot.keyClick(dialog, Qt.Key.Key_1, Qt.KeyboardModifier.AltModifier)
    assert not window.pending_sends
