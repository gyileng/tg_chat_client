"""Offline, disposable five-account workload; use the same script for both revisions."""

import argparse
import ctypes
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def memory():
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "peak_working_set",
                    "working_set",
                    "peak_paged",
                    "paged",
                    "peak_nonpaged",
                    "nonpaged",
                    "pagefile",
                    "peak_pagefile",
                    "private_bytes",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return {
            "working_set_mb": counters.working_set / 1048576,
            "private_mb": counters.private_bytes / 1048576,
        }
    # ps reports current resident memory, rather than the historical peak RSS.
    result = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())], check=True, capture_output=True, text=True
    )
    return {"rss_mb": int(result.stdout.strip()) / 1024}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package", type=Path, help="同时记录已构建压缩包的字节数")
    parser.add_argument("--messages-per-account", type=int, default=10000)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--idle-seconds", type=float, default=3)
    args = parser.parse_args()
    if args.package is not None and not args.package.is_file():
        parser.error("指定的压缩包不存在")
    if args.messages_per_account < 100 or args.samples < 5 or args.idle_seconds <= 0:
        parser.error("消息数量至少 100、切换采样至少 5、空闲采样时间须大于零")
    started = time.perf_counter()
    sys.path.insert(0, str(args.source_root.resolve()))
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon

    from tg_chat_client.models import ChatMessage
    from tg_chat_client.store import Store
    from tg_chat_client.ui import MainWindow

    class OfflineBridge(QObject):
        event = Signal(object)

        def submit(self, operation, **kwargs):
            return "benchmark-request"

        def close(self):
            pass

    app = QApplication([])
    QSystemTrayIcon.isSystemTrayAvailable = staticmethod(lambda: False)

    def pump(seconds=0):
        deadline = time.perf_counter() + seconds
        while True:
            app.processEvents()
            # A manually pumped loop must explicitly deliver deferred widget deletion.
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if time.perf_counter() >= deadline:
                return
            time.sleep(0.005)

    def idle_cpu():
        wall, cpu = time.perf_counter(), time.process_time()
        pump(args.idle_seconds)
        return 100 * (time.process_time() - cpu) / (time.perf_counter() - wall)

    with tempfile.TemporaryDirectory(prefix="tg-chat-benchmark-") as folder:
        store = Store(Path(folder))
        for i in range(5):
            store.add_account(str(i), f"模拟账号 {i}", "")
        window = MainWindow(store, OfflineBridge(), mock=True)
        try:
            window.groups_cache = {str(i): [{"id": -1, "title": "模拟压力群"}] for i in range(5)}
            window.statuses = {str(i): "模拟连接" for i in range(5)}
            window.show()
            pump(0.1)
            result = {
                "source_root": str(args.source_root.resolve()),
                "platform": sys.platform,
                "python": sys.version.split()[0],
                "qt_platform": app.platformName(),
                "ui_startup_ms": (time.perf_counter() - started) * 1000,
                "messages_per_account": args.messages_per_account,
            }
            if args.package is not None:
                result["package_bytes"] = args.package.stat().st_size
            for row in range(5):
                window.accounts.setCurrentRow(row)
                window.render_groups()
                pump()
            window.accounts.setCurrentRow(0)
            pump()
            mid = 0
            result["memory_initial"] = memory()
            result["memory_checkpoints"] = []
            for checkpoint in range(1, 6):
                end = math.ceil(args.messages_per_account * checkpoint / 5)
                for batch_end in range(mid + 100, end + 100, 100):
                    limit = min(batch_end, end)
                    for number in range(mid, limit):
                        for row in range(5):
                            message = ChatMessage(
                                str(row),
                                -1,
                                number,
                                "模拟群友",
                                "离线压力消息 " * 3,
                                "12:00",
                                outgoing=number % 7 == 0,
                                sender_id=42,
                            )
                            window.handle({"kind": "message", "message": message})
                    mid = limit
                    pump(0.02)
                result["memory_checkpoints"].append({"per_account_received": mid, **memory()})
                print(f"checkpoint: {mid} messages/account", flush=True)
            result["cached_recent_messages"] = sum(map(len, window.buffers.values()))
            result["foreground_cpu_percent_one_core"] = idle_cpu()
            window.hide()
            pump(0.1)
            result["background_cpu_percent_one_core"] = idle_cpu()
            window.show()
            pump(0.1)
            timings = []
            for sample in range(args.samples):
                before = time.perf_counter()
                window.accounts.setCurrentRow((sample + 1) % 5)
                pump()
                timings.append((time.perf_counter() - before) * 1000)
            result["switch_ms_median"] = statistics.median(timings)
            result["switch_ms_p95"] = sorted(timings)[math.ceil(len(timings) * 0.95) - 1]
            result["memory_final"] = memory()
            args.output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            window.request_exit()


if __name__ == "__main__":
    main()
