import sys

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QMessageBox, QPushButton

from tg_chat_client.app import report_ui_exception


def test_button_callback_failure_is_visible_without_logging_private_values(
    qtbot, monkeypatch, caplog
):
    button = QPushButton("连接设置")
    qtbot.addWidget(button)
    messages = []
    monkeypatch.setattr(sys, "excepthook", report_ui_exception)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: messages.append(args[2]))

    def broken_callback():
        raise RuntimeError("private-api-hash-and-chat-content")

    button.clicked.connect(broken_callback)
    button.click()
    assert len(messages) == 1
    assert "RuntimeError" in messages[0]
    assert "logs/client.log" in messages[0]
    assert "test_app.py:" in caplog.text
    assert "broken_callback" in caplog.text
    assert "private-api-hash-and-chat-content" not in caplog.text
    assert "private-api-hash-and-chat-content" not in messages[0]
