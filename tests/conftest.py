import pytest
from PySide6.QtWidgets import QSystemTrayIcon

from tg_chat_client.bridge import ServiceBridge
from tg_chat_client.store import Store
from tg_chat_client.ui import MainWindow


@pytest.fixture(autouse=True)
def no_desktop_tray(monkeypatch):
    # Keep all existing tests independent of the host desktop (including Windows CI).
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)


@pytest.fixture
def tray_window(window, monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: True)
    monkeypatch.setattr(QSystemTrayIcon, "show", lambda self: None)
    window.setup_tray()
    window.store.set_setting("tray_hint_seen", True)
    return window


@pytest.fixture
def window(qtbot, tmp_path):
    store = Store(tmp_path)
    bridge = ServiceBridge(store, mock=True)
    window = MainWindow(store, bridge, mock=True)
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    qtbot.waitUntil(
        lambda: window.accounts.count() == 5 and window.messages.model().rowCount() >= 30
    )
    yield window
    window.request_exit()
