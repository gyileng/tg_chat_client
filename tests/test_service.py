import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telethon.errors import FloodWaitError, SlowModeWaitError

from tg_chat_client.models import ChatMessage, SendRequest
from tg_chat_client.service import MockService, ServiceRuntime, TelegramService, prune_cache
from tg_chat_client.store import Store


class ControlledService:
    def __init__(self):
        self.release = asyncio.Event()
        self.calls = []
        self.error = None

    async def send(self, request):
        self.calls.append(request)
        await self.release.wait()
        if self.error:
            raise self.error
        return ChatMessage(
            request.account_id,
            request.chat_id,
            len(self.calls),
            "我",
            request.text,
            "12:00",
            True,
            request.reply_to,
        )


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.events = []
        self.service = ControlledService()
        self.runtime = ServiceRuntime(self.store, self.service, self.events.append)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def request(self, request_id="r", aid="a", chat=-1):
        return SendRequest(request_id, aid, chat, "文字", 12)

    async def send(self, request):
        await self.runtime.execute("send", request.request_id, {"request": request})

    async def test_second_send_to_busy_target_is_rejected(self):
        first = asyncio.create_task(self.send(self.request()))
        await asyncio.sleep(0)
        await self.send(self.request("second"))
        self.assertEqual(self.events[-1]["kind"], "error")
        self.service.release.set()
        await first
        self.assertEqual(len(self.service.calls), 1)
        self.assertEqual(self.store.count("a", -1), 1)

    async def test_other_account_can_send_and_frozen_target_is_preserved(self):
        first = asyncio.create_task(self.send(self.request()))
        second = asyncio.create_task(self.send(self.request("second", "b", -2)))
        await asyncio.sleep(0)
        self.service.release.set()
        await asyncio.gather(first, second)
        self.assertEqual(
            {(r.account_id, r.chat_id, r.reply_to) for r in self.service.calls},
            {("a", -1, 12), ("b", -2, 12)},
        )
        self.assertEqual(self.store.count("a", -1), 1)
        self.assertEqual(self.store.count("b", -2), 1)

    async def test_network_failure_is_unknown_and_never_retried(self):
        self.service.error = OSError("private credential must not leak")
        self.service.release.set()
        await self.send(self.request())
        self.assertEqual(len(self.service.calls), 1)
        self.assertNotIn("private credential", self.events[-1]["message"])
        self.assertEqual(self.store.count("a"), 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM sends").fetchone()[0], "unknown")

    async def test_flood_wait_returns_countdown_and_does_not_schedule_retry(self):
        self.service.error = FloodWaitError(request=None, capture=30)
        self.service.release.set()
        await self.send(self.request())
        self.assertEqual(self.events[-1]["wait"], 30)
        self.assertEqual(len(self.service.calls), 1)
        self.assertEqual(self.store.count("a"), 0)

    async def test_connection_changes_are_rejected_while_sending(self):
        task = asyncio.create_task(self.send(self.request()))
        await asyncio.sleep(0)
        await self.runtime.execute("reconnect", "reconnect", {})
        self.assertEqual(self.events[-1]["kind"], "error")
        self.service.release.set()
        await task

    async def test_mock_mode_seeds_five_accounts_and_quote_is_preserved(self):
        mock = MockService(self.store, self.events.append)
        await mock.start()
        self.assertEqual(len(self.store.accounts()), 5)
        message = await mock.send(self.request(aid="mock-1", chat=-1001))
        self.assertEqual(message.reply_to, 12)
        self.assertTrue(message.outgoing)
        await mock.stop()

    async def test_missing_proxy_config_never_creates_direct_client(self):
        service = TelegramService(self.store, self.events.append)
        with self.assertRaises(ValueError):
            service.client("a")

    async def test_missing_proxy_dependency_is_fatal(self):
        from tg_chat_client.models import ProxyConfig

        self.store.save_connection(123, "test-only-hash", ProxyConfig())
        service = TelegramService(self.store, self.events.append)
        with patch.dict("sys.modules", {"python_socks": None}):
            with self.assertRaisesRegex(ValueError, "python-socks"):
                service.client("a")

    async def test_canceled_send_becomes_unknown_without_retry(self):
        task = asyncio.create_task(self.send(self.request()))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM sends").fetchone()[0], "unknown")
        self.assertEqual(len(self.service.calls), 1)
        self.assertFalse(self.runtime.busy)

    async def test_flood_wait_is_also_enforced_by_service(self):
        self.service.error = FloodWaitError(request=None, capture=30)
        self.service.release.set()
        await self.send(self.request())
        await self.send(self.request("second", chat=-2))
        self.assertEqual(len(self.service.calls), 1)
        self.assertEqual(self.store.count("a"), 0)

    async def test_slow_mode_blocks_only_same_group(self):
        self.service.error = SlowModeWaitError(request=None, capture=30)
        self.service.release.set()
        await self.send(self.request())
        self.assertEqual(self.events[-1]["wait_scope"], "target")
        await self.send(self.request("blocked"))
        self.service.error = None
        await self.send(self.request("other", chat=-2))
        self.assertEqual(len(self.service.calls), 2)
        self.assertEqual(self.store.count("a", -2), 1)

    async def test_mock_preview_has_valid_png_chunks(self):
        import struct
        import zlib

        mock = MockService(self.store, self.events.append)
        data = Path(await mock.preview("mock-1", -1001, 5)).read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        offset = 8
        while offset < len(data):
            size = struct.unpack(">I", data[offset : offset + 4])[0]
            payload = data[offset + 4 : offset + 8 + size]
            checksum = struct.unpack(">I", data[offset + 8 + size : offset + 12 + size])[0]
            self.assertEqual(checksum, zlib.crc32(payload))
            offset += 12 + size

    async def test_cache_prunes_oldest_files_to_budget(self):
        folder = self.store.root / "cache"
        (folder / "a").write_bytes(b"a" * 10)
        (folder / "b").write_bytes(b"b" * 10)
        prune_cache(self.store.root, max_bytes=10)
        self.assertLessEqual(sum(p.stat().st_size for p in folder.iterdir()), 10)
