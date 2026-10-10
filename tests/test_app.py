import os
import subprocess
import sys

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QMessageBox, QPushButton

from tg_chat_client.app import report_ui_exception


@pytest.mark.parametrize("exit_mode", ["smoke", "external"])
def test_application_exits_with_tray_and_releases_instance_lock(tmp_path, exit_mode):
    script = """
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from tg_chat_client.app import main
from tg_chat_client.ui import MainWindow
QSystemTrayIcon.isSystemTrayAvailable = staticmethod(lambda: True)
QSystemTrayIcon.show = lambda self: None
windows = []
original_setup = MainWindow.setup_tray
def setup(self):
    original_setup(self)
    windows.append(self)
    if sys.argv[2] == 'external':
        QTimer.singleShot(200, QApplication.instance().quit)
MainWindow.setup_tray = setup
args = ['--mock', '--data-dir', sys.argv[1]]
if sys.argv[2] == 'smoke':
    args.append('--smoke-test')
assert main(args) == 0
assert windows[0].closing and windows[0].bridge.closed
"""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    # A second launch using the same data directory must also succeed.
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), exit_mode],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr


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
