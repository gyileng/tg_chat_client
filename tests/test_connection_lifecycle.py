import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tg_chat_client.models import SendRequest
from tg_chat_client.service import (
    BACKGROUND_DISCONNECT_SECONDS,
    MockService,
    ServiceRuntime,
    TelegramService,
)
from tg_chat_client.store import Store


class ConnectionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.events = []
        self.service = MockService(self.store, self.events.append)
        await self.service.start()
        self.runtime = ServiceRuntime(self.store, self.service, self.events.append)

    async def asyncTearDown(self):
        if self.runtime.connection_task:
            self.runtime.connection_task.cancel()
            await asyncio.gather(self.runtime.connection_task, return_exceptions=True)
        await self.service.stop()
        self.temp.cleanup()

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0)

        await asyncio.wait_for(wait(), 2)

    async def expire(self):
        self.runtime.set_foreground(False)
        self.runtime.background_since -= BACKGROUND_DISCONNECT_SECONDS
        self.runtime.visibility_changed.set()
        await self.until(lambda: self.runtime.connection_state != "online")

    async def test_grace_period_reset_and_repeated_background_events(self):
        self.assertEqual(self.runtime.background_delay, 300)
        loop = asyncio.get_running_loop()
        started = loop.time()
        # Control only synchronous clock reads; keep the event loop's timers real.
        with patch.object(loop, "time", return_value=started):
            self.runtime.set_foreground(False)
        with patch.object(loop, "time", return_value=started + 60):
            self.runtime.set_foreground(False)
        self.assertEqual(self.runtime.background_since, started)
        await asyncio.sleep(0)
        self.assertTrue(self.service.connected)
        self.assertEqual(self.runtime.connection_state, "online")
        self.runtime.set_foreground(True)
        await asyncio.sleep(0)
        self.assertIsNone(self.runtime.background_since)
        with patch.object(loop, "time", return_value=started + 120):
            self.runtime.set_foreground(False)
        self.assertEqual(self.runtime.background_since, started + 120)
        self.assertTrue(self.service.connected)

    async def test_foreground_round_trip_can_share_clock_tick(self):
        loop = asyncio.get_running_loop()
        now = loop.time()
        with patch.object(loop, "time", return_value=now):
            self.runtime.set_foreground(False)
            self.runtime.set_foreground(True)
            self.assertIsNone(self.runtime.background_since)
            self.runtime.set_foreground(False)
        self.assertEqual(self.runtime.background_since, now)
        self.assertFalse(self.runtime.foreground)
        self.assertTrue(self.service.connected)

    async def test_disconnect_stays_offline_and_resume_retains_messages_and_login_state(self):
        await self.service.logout("mock-2")
        request = SendRequest("r", "mock-1", -1001, "断开前已发送")
        await self.runtime.execute("send", "r", {"request": request})
        before = list(self.service.messages["mock-1", -1001])
        await self.expire()
        await self.until(lambda: self.runtime.connection_state == "paused")
        self.assertFalse(self.service.connected)
        self.assertIsNone(self.service.ticker)
        await self.runtime.execute("send", "blocked", {"request": request})
        self.assertEqual(self.events[-1]["kind"], "error")
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(self.service.connected)
        self.runtime.set_foreground(True)
        self.runtime.set_foreground(True)
        await self.until(lambda: self.runtime.connection_state == "online")
        self.assertTrue(self.service.connected)
        self.assertEqual(self.service.messages["mock-1", -1001], before)
        self.assertEqual(self.service.mock_status("mock-2"), "需要登录")
        self.assertEqual(self.store.count("mock-1"), 1)

    async def test_pending_send_finishes_before_disconnect_and_rejects_new_sends(self):
        release = asyncio.Event()
        original_send = self.service.send

        async def send(request):
            await release.wait()
            return await original_send(request)

        self.service.send = AsyncMock(side_effect=send)
        request = SendRequest("r", "mock-1", -1001, "等待回执")
        task = asyncio.create_task(self.runtime.execute("send", "r", {"request": request}))
        await self.until(lambda: bool(self.runtime.busy))
        await self.expire()
        self.assertEqual(self.runtime.connection_state, "pausing")
        self.assertTrue(self.service.connected)
        await self.runtime.execute("send", "second", {"request": request})
        self.assertEqual(self.events[-1]["kind"], "error")
        release.set()
        await task
        await self.until(lambda: self.runtime.connection_state == "paused")
        self.service.send.assert_awaited_once()
        self.assertEqual(self.store.count("mock-1"), 1)

    async def test_login_and_history_are_canceled_at_deadline(self):
        waiting = asyncio.Event()

        async def login(**kwargs):
            await waiting.wait()

        self.service.request_code = AsyncMock(side_effect=login)
        login_task = asyncio.create_task(
            self.runtime.execute("request_code", "login", {"phone": "+123"})
        )
        await self.until(lambda: self.service.request_code.await_count == 1)
        await self.expire()
        await self.until(lambda: self.runtime.connection_state == "paused")
        self.assertTrue(login_task.cancelled())
        self.assertFalse(self.runtime.control_lock.locked())
        self.runtime.set_foreground(True)
        await self.until(lambda: self.runtime.connection_state == "online")
        self.service.history = AsyncMock(side_effect=login)
        history_task = asyncio.create_task(
            self.runtime.execute("history", "history", {"account_id": "mock-1", "chat_id": -1001})
        )
        await self.until(lambda: self.service.history.await_count == 1)
        await self.expire()
        await self.until(lambda: self.runtime.connection_state == "paused")
        self.assertTrue(history_task.cancelled())

    async def test_restore_during_disconnect_reconnects_once(self):
        release = asyncio.Event()
        original_stop = self.service.stop

        async def stop():
            await release.wait()
            await original_stop()

        self.service.stop = AsyncMock(side_effect=stop)
        self.service.start = AsyncMock(wraps=self.service.start)
        await self.expire()
        await self.until(lambda: self.service.stop.await_count == 1)
        self.runtime.set_foreground(True)
        self.runtime.set_foreground(True)
        release.set()
        await self.until(lambda: self.runtime.connection_state == "online")
        self.service.stop.assert_awaited_once()
        self.service.start.assert_awaited_once()
        self.assertTrue(self.service.connected)

    async def test_resume_failure_cleans_up_and_allows_manual_retry(self):
        await self.expire()
        await self.until(lambda: self.runtime.connection_state == "paused")
        original_start = self.service.start

        async def partial_start():
            await original_start()
            raise OSError("private configuration")

        self.service.start = AsyncMock(side_effect=partial_start)
        self.runtime.set_foreground(True)
        await self.until(lambda: self.runtime.connection_state == "online")
        self.assertFalse(self.service.connected)
        self.assertIsNone(self.service.ticker)
        self.assertTrue(any(e.get("state") == "resume_failed" for e in self.events))
        self.service.start.assert_awaited_once()
        self.service.start = original_start
        await self.runtime.execute("reconnect", "retry", {})
        self.assertTrue(self.service.connected)

    async def test_real_clients_disconnect_and_monitor_is_removed(self):
        await self.service.stop()
        self.service = TelegramService(self.store, self.events.append)
        self.runtime.service = self.service
        for account in self.store.accounts():
            (self.store.root / "sessions" / (account["id"] + ".session")).touch()
        clients = []

        def client(aid):
            instance = Mock()
            instance.connect = AsyncMock()
            instance.disconnect = AsyncMock()
            instance.is_user_authorized = AsyncMock(return_value=aid != "mock-2")
            clients.append(instance)
            return instance

        with patch.object(self.service, "client", side_effect=client):
            await self.service.start()
            initial = list(self.service.clients.values())
            await self.expire()
            await self.until(lambda: self.runtime.connection_state == "paused")
            self.assertFalse(self.service.clients)
            self.assertIsNone(self.service.monitor)
            for instance in initial:
                instance.disconnect.assert_awaited_once()
            self.runtime.set_foreground(True)
            await self.until(lambda: self.runtime.connection_state == "online")
            self.assertEqual(len(self.service.clients), 4)
            self.assertNotIn("mock-2", self.service.clients)
            for instance in self.service.clients.values():
                instance.add_event_handler.assert_called_once()

    async def test_shutdown_cancels_lifecycle_without_resuming(self):
        await self.expire()
        await self.until(lambda: self.runtime.connection_state == "paused")
        self.service.start = AsyncMock(wraps=self.service.start)
        self.runtime.connection_task.cancel()
        await asyncio.gather(self.runtime.connection_task, return_exceptions=True)
        await self.service.stop()
        self.assertFalse(self.service.connected)
        self.service.start.assert_not_awaited()

    async def test_account_removal_finishes_before_disconnect(self):
        release = asyncio.Event()
        original_remove = self.service.remove_account

        async def remove(account_id):
            await release.wait()
            await original_remove(account_id)

        self.service.remove_account = AsyncMock(side_effect=remove)
        task = asyncio.create_task(
            self.runtime.execute("remove_account", "remove", {"account_id": "mock-1"})
        )
        await self.until(lambda: self.service.remove_account.await_count == 1)
        await self.expire()
        self.assertFalse(task.done())
        self.assertTrue(self.service.connected)
        release.set()
        await task
        await self.until(lambda: self.runtime.connection_state == "paused")
        self.assertNotIn("mock-1", {a["id"] for a in self.store.accounts()})

    async def test_connect_request_is_canceled_and_cleaned_up_at_deadline(self):
        release = asyncio.Event()

        async def reconnect():
            await release.wait()

        self.service.reconnect = AsyncMock(side_effect=reconnect)
        task = asyncio.create_task(self.runtime.execute("reconnect", "reconnect", {}))
        await self.until(lambda: self.service.reconnect.await_count == 1)
        await self.expire()
        await self.until(lambda: self.runtime.connection_state == "paused")
        self.assertTrue(task.cancelled())
        self.assertFalse(self.service.connected)

    async def test_hiding_again_during_resume_respects_new_deadline(self):
        release = asyncio.Event()
        original_start = self.service.start

        async def start():
            await release.wait()
            await original_start()

        await self.expire()
        await self.until(lambda: self.runtime.connection_state == "paused")
        self.service.start = AsyncMock(side_effect=start)
        self.runtime.set_foreground(True)
        await self.until(lambda: self.runtime.connection_state == "resuming")
        self.runtime.set_foreground(False)
        self.runtime.background_since -= BACKGROUND_DISCONNECT_SECONDS
        release.set()
        await self.until(lambda: self.runtime.connection_state == "paused")
        self.assertFalse(self.service.connected)
        self.service.start.assert_awaited_once()
