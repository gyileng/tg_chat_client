import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .bridge import ServiceBridge
from .service import prune_cache
from .store import Store
from .ui import MainWindow


def data_root(mock=False):
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "TGChatClient" / ("mock" if mock else "real")


def main(argv=None):
    parser = argparse.ArgumentParser(description="TG Chat Client")
    parser.add_argument("--mock", action="store_true", help="仅使用模拟账号，不连接 Telegram")
    parser.add_argument("--data-dir", type=Path, help="指定本地数据根目录（模拟/真实仍分开）")
    parser.add_argument(
        "--smoke-test", action="store_true", help="模拟模式启动后自动退出，供构建验证"
    )
    args = parser.parse_args(argv)
    if args.smoke_test and not args.mock:
        parser.error("--smoke-test 必须与 --mock 一起使用")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("TGChatClient")
    root = (
        args.data_dir / ("mock" if args.mock else "real") if args.data_dir else data_root(args.mock)
    )
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        QMessageBox.critical(None, "无法启动", "无法创建本地数据目录，请检查目录访问权限。")
        return 1
    lock = QLockFile(str(root / "client.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "程序已运行", "相同数据目录的程序已运行，请使用已有窗口。")
        return 1
    bridge = None
    try:
        store = Store(root)
        handler = RotatingFileHandler(
            root / "logs" / "client.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.basicConfig(level=logging.WARNING, handlers=[handler])
        # Third-party exceptions can contain private arguments; collect only our sanitized events.
        logging.getLogger("telethon").disabled = True
        logging.getLogger("telethon").addHandler(logging.NullHandler())
        logging.getLogger("telethon").propagate = False
        prune_cache(root)
        bridge = ServiceBridge(store, args.mock)
        window = MainWindow(store, bridge, args.mock)
        window.show()
        if args.smoke_test:
            QTimer.singleShot(3000, window.close)
        return app.exec()
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, RuntimeError)
            else "初始化失败，请检查数据目录或重新安装完整程序包。"
        )
        QMessageBox.critical(None, "无法启动", message)
        return 1
    finally:
        if bridge:
            bridge.close()
        lock.unlock()
