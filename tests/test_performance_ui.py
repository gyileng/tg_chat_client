import time
from dataclasses import replace

import pytest

pytest.importorskip("PySide6")
from PySide6.QtTest import QAbstractItemModelTester
from PySide6.QtWidgets import QWidget

from tg_chat_client.message_cache import HISTORY_LIMIT, RECENT_LIMIT
from tg_chat_client.message_view import MessageDelegate, MessageModel
from tg_chat_client.models import ChatMessage, SendRequest


@pytest.mark.parametrize(
    "media,label",
    [
        ("photo", "图片"),
        ("sticker", "贴纸"),
        ("video", "视频"),
        ("audio", "音频"),
        ("file", "文件"),
        ("other", "其他媒体"),
    ],
)
def test_all_media_keep_caption_and_placeholder_without_preview(window, media, label):
    message = ChatMessage(*window.target, 9999, "<群友>", "附带文字", "12:00", media=media)
    window.put_message(message)
    window.render_messages()
    delegate = window.messages.itemDelegate()
    assert isinstance(delegate, MessageDelegate)
    doc, _ = delegate.layout(message, window.messages.font())
    assert f"[{label}]" in doc.toPlainText()
    assert "附带文字" in doc.toPlainText()
    assert "<群友>" in doc.toPlainText()
    window.quote(window.messages.model().index_for(9999))
    assert window.reply_id == 9999
    assert not list((window.store.root / "cache").iterdir())


def test_model_incremental_changes_are_valid_and_do_not_reset(qtbot):
    model = MessageModel()
    tester = QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Warning)
    resets, inserts, changes = [], [], []
    model.modelReset.connect(lambda: resets.append(True))
    model.rowsInserted.connect(lambda *args: inserts.append(True))
    model.dataChanged.connect(lambda *args: changes.append(True))
    messages = {mid: ChatMessage("a", -1, mid, "群友", "正文", "12:00") for mid in (10, 20)}
    model.sync(("a", -1), messages)
    messages[5] = replace(messages[10], message_id=5)
    messages[30] = replace(messages[10], message_id=30)
    messages[20] = replace(messages[20], text="更正")
    model.sync(("a", -1), messages)
    del messages[10]
    model.sync(("a", -1), messages)
    assert model.ids == [5, 20, 30]
    assert len(resets) == 1 and len(inserts) == 2 and len(changes) == 1
    assert tester.model() is model


def test_short_message_uses_natural_width_and_keeps_whitespace(window):
    message = ChatMessage(*window.target, 9999, "群友", "附带说明文字  两个空格", "12:00")
    doc, _ = window.messages.itemDelegate().layout(message, window.messages.font())
    assert "附带说明文字  两个空格" in doc.toPlainText()
    assert doc.findBlockByNumber(1).layout().lineCount() == 1


def test_resize_at_live_bottom_does_not_enter_history(window, qtbot):
    window.go_bottom(refresh=False)
    for width in (900, 1100, 850):
        window.resize(width, 650)
        qtbot.wait(50)
        assert window.target not in window.cache.history
        bar = window.messages.verticalScrollBar()
        assert bar.value() == bar.maximum()
    sample = next(iter(window.buffers[window.target].values()))
    window.handle({"kind": "message", "message": replace(sample, message_id=9000)})
    qtbot.wait(50)
    assert window.target not in window.cache.history
    index = window.messages.model().index_for(9000)
    assert window.messages.visualRect(index).intersects(window.messages.viewport().rect())


@pytest.mark.parametrize("state", ["hidden", "minimized"])
def test_background_has_no_model_layout_or_database_refresh(tray_window, qtbot, monkeypatch, state):
    window = tray_window
    old_rows = window.messages.model().rowCount()
    sample = next(iter(window.buffers[window.target].values()))
    queries, inserts, paints = [], [], []
    monkeypatch.setattr(
        window.store, "counts_for_accounts", lambda *args: queries.append(True) or {}
    )
    monkeypatch.setattr(window.store, "accounts", lambda: pytest.fail("后台消息不应查询账号"))
    window.messages.model().rowsInserted.connect(lambda *args: inserts.append(True))
    monkeypatch.setattr(window.messages.itemDelegate(), "paint", lambda *args: paints.append(True))
    if state == "hidden":
        window.close()
    else:
        window.showMinimized()
    qtbot.wait(30)
    for mid in range(1000, 11000):
        window.handle({"kind": "message", "message": replace(sample, message_id=mid)})
    qtbot.wait(1100)
    assert not window.timer.isActive()
    assert not window.message_resize_timer.isActive()
    assert not window.message_sync_timer.isActive()
    assert not queries and not inserts and not paints
    assert window.messages.model().rowCount() == old_rows
    assert len(window.buffers[window.target]) == RECENT_LIMIT
    window.restore_window()
    qtbot.wait(30)
    assert window.messages.model().rowCount() == RECENT_LIMIT
    assert not window.messages.viewport().findChildren(QWidget)


def test_regular_messages_and_countdown_do_not_query_counts(window, qtbot, monkeypatch):
    queries = []
    original = window.store.counts_for_accounts
    monkeypatch.setattr(
        window.store, "counts_for_accounts", lambda *args: queries.append(True) or original(*args)
    )
    sample = next(iter(window.buffers[window.target].values()))
    window.handle({"kind": "message", "message": replace(sample, message_id=999)})
    qtbot.wait(40)
    assert not queries
    window.blocked[window.target] = time.monotonic() + 1.1
    window.refresh_counts()
    assert window.timer.isActive()
    qtbot.waitUntil(lambda: not window.timer.isActive(), timeout=3500)
    assert not queries
    window.input.setPlainText("一次手动发送")
    window.send_text(window.input.toPlainText(), "input")
    qtbot.waitUntil(lambda: not window.pending_sends)
    assert len(queries) == 1


@pytest.mark.parametrize("finish", ["restore", "exit"])
def test_midnight_and_hidden_send_failure_preserve_draft(tray_window, qtbot, monkeypatch, finish):
    window = tray_window
    window.count_day = "2000-01-01"
    window.close()
    queries = []
    original = window.store.counts_for_accounts
    monkeypatch.setattr(
        window.store, "counts_for_accounts", lambda *args: queries.append(True) or original(*args)
    )
    request = SendRequest("failure", *window.target, "未成功的原句")
    window.pending_sends[window.target] = (request, "input", "")
    window.input.setPlainText("下一条")
    window.day_changed()
    window.handle({"kind": "error", "operation": "send", "request": request, "message": "失败"})
    assert not queries
    assert window.store.draft(*window.target)["text"] == "未成功的原句\n下一条"
    if finish == "restore":
        window.restore_window()
        assert window.input.toPlainText() == "未成功的原句\n下一条"
        assert len(queries) == 1
    else:
        window.request_exit()
        assert window.store.draft(*window.target)["text"] == "未成功的原句\n下一条"
        assert not queries


def test_history_paging_preserves_anchor_and_ignores_old_reply_after_bottom(
    window, qtbot, monkeypatch
):
    target = window.target
    sample = next(iter(window.buffers[target].values()))
    for mid in range(1000, 1600):
        window.put_message(replace(sample, message_id=mid))
    window.render_messages()
    window.messages.scrollTo(
        window.messages.model().index(100), window.messages.ScrollHint.PositionAtTop
    )
    anchor = window.messages.anchor()
    monkeypatch.setattr(window.bridge, "submit", lambda *args, **kwargs: "older-request")
    window.history_requests.clear()
    window.load_history(older=True)
    before = window.history_before[target]
    page = [replace(sample, message_id=mid) for mid in range(before - 50, before)]
    window.handle(
        {
            "kind": "result",
            "operation": "history",
            "account_id": target[0],
            "chat_id": target[1],
            "request_id": "older-request",
            "before": before,
            "result": page,
        }
    )
    assert window.messages.anchor() == anchor
    assert len(window.cache.view(target)) == HISTORY_LIMIT
    window.load_history(older=True)
    window.go_bottom(refresh=False)
    window.handle(
        {
            "kind": "result",
            "operation": "history",
            "account_id": target[0],
            "chat_id": target[1],
            "request_id": "older-request",
            "before": before,
            "result": page,
        }
    )
    assert target not in window.cache.history
    assert max(window.cache.view(target)) == 1599
    window.quote(window.messages.model().index(0))
    assert window.reply_id == 1100
