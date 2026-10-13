import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon.errors import FloodWaitError, PhoneMigrateError, SlowModeWaitError

from tg_chat_client.models import ChatMessage, SendRequest
from tg_chat_client.service import MockService, ServiceRuntime, TelegramService
from tg_chat_client.store import Store


class ControlledService:
    def __init__(self):
        self.release = asyncio.Event()
        self.calls = []
        self.error = None

    async def send(self, request):
        self.calls.append(request)
        message_id = len(self.calls)
        await self.release.wait()
        if self.error:
            raise self.error
        return ChatMessage(
            request.account_id,
            request.chat_id,
            message_id,
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

    async def test_sender_identity_survives_failed_name_lookup(self):
        service = TelegramService(self.store, self.events.append)
        for sender_id in (42, -100123, None):
            message = SimpleNamespace(
                get_sender=AsyncMock(side_effect=OSError("name unavailable")),
                sender_id=sender_id,
                chat_id=-1,
                id=1,
                raw_text="正文",
                date=datetime.now(timezone.utc),
                out=False,
                reply_to_msg_id=None,
                photo=None,
                sticker=None,
                video=None,
                gif=None,
                voice=None,
                audio=None,
                document=None,
                media=None,
            )
            result = await service.convert("a", message)
            self.assertEqual(result.sender_id, sender_id)
            self.assertEqual(result.sender, "未知发送者")
            self.assertEqual(result.text, "正文")

    async def test_send_uses_returned_identity_without_extra_lookup(self):
        service = TelegramService(self.store, self.events.append)
        message = SimpleNamespace(
            id=1,
            sender_id=42,
            date=datetime.now(timezone.utc),
            get_sender=AsyncMock(side_effect=OSError("must not query")),
        )
        client = SimpleNamespace(send_message=AsyncMock(return_value=message))
        with patch.object(service, "authorized", return_value=client):
            request = SendRequest("r", "a", -1, "文字", 12, "群友：引用原文")
            result = await service.send(request)
        self.assertEqual(result.sender_id, 42)
        self.assertTrue(result.outgoing)
        self.assertEqual(result.reply_text, "群友：引用原文")
        client.send_message.assert_awaited_once()
        message.get_sender.assert_not_awaited()

    def telegram_message(self, mid, text, reply_id=None, **kwargs):
        from telethon.tl.types import User

        return SimpleNamespace(
            id=mid,
            chat_id=-1,
            sender_id=42,
            get_sender=AsyncMock(return_value=User(42, first_name="群友", last_name="全名")),
            get_reply_message=AsyncMock(return_value=None),
            raw_text=text,
            date=datetime.now(timezone.utc),
            out=False,
            reply_to_msg_id=reply_id,
            **{
                **dict.fromkeys(
                    ("photo", "sticker", "video", "gif", "voice", "audio", "document", "media")
                ),
                **kwargs,
            },
        )

    async def test_history_resolves_quotes_in_page_and_batches_missing_originals(self):
        service = TelegramService(self.store, self.events.append)
        page = [
            self.telegram_message(4, "回复二", 1),
            self.telegram_message(3, "回复一", 1),
            self.telegram_message(2, "回复本页", 3),
        ]
        original = self.telegram_message(1, "<原文>\n第二行")
        client = SimpleNamespace(get_messages=AsyncMock(side_effect=[page, [original]]))
        with patch.object(service, "authorized", return_value=client):
            result = await service.history("a", -1)
        self.assertEqual([m.message_id for m in result], [2, 3, 4])
        self.assertEqual(result[0].reply_text, "群友 全名：回复一")
        self.assertEqual(result[1].reply_text, "群友 全名：<原文>\n第二行")
        self.assertEqual(result[2].reply_text, result[1].reply_text)
        self.assertEqual(client.get_messages.await_count, 2)
        client.get_messages.assert_awaited_with(-1, ids=[1])
        for message in page:
            message.get_reply_message.assert_not_awaited()

    async def test_missing_or_failed_quote_lookup_keeps_history_and_incoming_message(self):
        service = TelegramService(self.store, self.events.append)
        message = self.telegram_message(2, "正文", 1)
        for failure in ([None], OSError("unavailable"), FloodWaitError(None, capture=30)):
            client = SimpleNamespace(get_messages=AsyncMock(side_effect=[[message], failure]))
            with patch.object(service, "authorized", return_value=client):
                result = await service.history("a", -1)
            self.assertEqual(result[0].text, "正文")
            self.assertEqual(result[0].reply_text, "")
        message.get_reply_message.side_effect = OSError("unavailable")
        result = await service.convert("a", message)
        self.assertEqual(result.text, "正文")

    async def test_incoming_quote_preserves_media_placeholder_without_nested_lookup(self):
        service = TelegramService(self.store, self.events.append)
        original = self.telegram_message(1, "", 99, photo=True)
        message = self.telegram_message(2, "正文", 1)
        message.get_reply_message.return_value = original
        result = await service.convert("a", message)
        self.assertEqual(result.reply_text, "群友 全名：[图片]")
        original.get_reply_message.assert_not_awaited()

    async def test_external_quote_never_uses_same_number_in_current_chat(self):
        service = TelegramService(self.store, self.events.append)
        other = self.telegram_message(1, "另一个群的原文")
        message = self.telegram_message(2, "正文", 1, reply_to=SimpleNamespace(reply_to_peer_id=42))
        message.get_reply_message.return_value = other
        client = SimpleNamespace(
            get_messages=AsyncMock(return_value=[message, self.telegram_message(1, "本群同号")])
        )
        with patch.object(service, "authorized", return_value=client):
            result = await service.history("a", -1)
        self.assertEqual(result[1].reply_text, "群友 全名：另一个群的原文")
        client.get_messages.assert_awaited_once()

    async def test_sender_display_name_includes_last_name_and_preserves_fallbacks(self):
        from telethon.tl.types import ChatForbidden, User

        service = TelegramService(self.store, self.events.append)
        cases = (
            (User(42, first_name="Ada", last_name="Lovelace", username="alias"), "Ada Lovelace"),
            (User(42, first_name="名字", last_name="另一半"), "名字 另一半"),
            (User(42, first_name="只有名字"), "只有名字"),
            (User(42, last_name="只有姓氏"), "只有姓氏"),
            (User(42, username="alias"), "alias"),
            (ChatForbidden(42, "群组全名"), "群组全名"),
            (None, "未知发送者"),
        )
        for sender, expected in cases:
            message = SimpleNamespace(
                get_sender=AsyncMock(return_value=sender),
                sender_id=42,
                chat_id=-1,
                id=1,
                raw_text="正文",
                date=datetime.now(timezone.utc),
                out=False,
                reply_to_msg_id=None,
                **{
                    kind: None
                    for kind in (
                        "photo",
                        "sticker",
                        "video",
                        "gif",
                        "voice",
                        "audio",
                        "document",
                        "media",
                    )
                },
            )
            result = await service.convert("a", message)
            self.assertEqual(result.sender, expected)
            self.assertEqual(result.text, "正文")
            self.assertEqual(result.sender_id, 42)

    async def test_login_account_label_uses_full_name(self):
        from telethon.tl.types import User

        service = TelegramService(self.store, self.events.append)
        client = Mock()
        client.sign_in = AsyncMock()
        client.get_me = AsyncMock(return_value=User(42, first_name="Ada", last_name="Lovelace"))
        service.clients["a"] = client
        service.codes["a"] = {"phone": "+123", "hash": "test"}
        await service.sign_in("a", code="12345")
        self.assertEqual(self.store.accounts()[0]["label"], "Ada Lovelace")

    async def test_multiple_sends_to_same_target_are_tracked_independently(self):
        first = asyncio.create_task(self.send(self.request()))
        second = asyncio.create_task(self.send(self.request("second")))
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(len(self.service.calls), 2)
        self.assertEqual(len(self.runtime.busy), 2)
        self.service.release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.store.count("a", -1), 2)
        self.assertFalse(self.runtime.busy)

    async def test_one_send_finishing_does_not_release_other_send_protection(self):
        releases = {rid: asyncio.Event() for rid in ("first", "second")}

        async def send(request):
            await releases[request.request_id].wait()
            return ChatMessage(
                "a",
                -1,
                1 if request.request_id == "first" else 2,
                "我",
                request.text,
                "12:00",
                True,
            )

        self.service.send = send
        first = asyncio.create_task(self.send(self.request("first")))
        second = asyncio.create_task(self.send(self.request("second")))
        await asyncio.sleep(0)
        releases["second"].set()
        await second
        await self.runtime.execute("reconnect", "reconnect", {})
        self.assertIn("等待发送", self.events[-1]["message"])
        releases["first"].set()
        await first
        self.assertEqual(self.store.count("a", -1), 2)

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

    async def test_account_changes_are_rejected_while_sending(self):
        task = asyncio.create_task(self.send(self.request()))
        await asyncio.sleep(0)
        for operation in ("logout", "remove_account"):
            await self.runtime.execute(operation, operation, {"account_id": "a"})
            self.assertIn("等待发送", self.events[-1]["message"])
        self.service.release.set()
        await task

    async def test_logout_revokes_session_and_preserves_draft(self):
        self.store.add_account("a", "A", "+123")
        self.store.save_draft("a", -1, "保留")
        path = self.store.root / "sessions" / "a.session"
        path.write_text("fixture")
        client = Mock()
        client.is_connected.return_value = True
        client.is_user_authorized = AsyncMock(return_value=True)
        client.log_out = AsyncMock(return_value=True)
        client.disconnect = AsyncMock()
        service = TelegramService(self.store, self.events.append)
        service.clients["a"] = client
        await service.logout("a")
        client.log_out.assert_awaited_once()
        client.session.close.assert_called_once()
        self.assertFalse(path.exists())
        self.assertEqual(self.store.draft("a", -1)["text"], "保留")
        self.assertEqual(len(self.store.accounts()), 1)
        await service.reconnect()
        self.assertEqual(self.events[-1]["status"], "需要登录")
        await service.stop()

    async def test_failed_logout_preserves_account_session_and_draft(self):
        self.store.add_account("a", "A", "+123")
        self.store.save_draft("a", -1, "保留")
        path = self.store.root / "sessions" / "a.session"
        path.write_text("fixture")
        client = Mock()
        client.is_connected.return_value = False
        client.connect = AsyncMock()
        client.is_user_authorized = AsyncMock(return_value=True)
        client.log_out = AsyncMock(return_value=False)
        client.disconnect = AsyncMock()
        service = TelegramService(self.store, self.events.append)
        with patch.object(service, "client", return_value=client):
            with self.assertRaises(ValueError):
                await service.remove_account("a")
        self.assertTrue(path.exists())
        self.assertEqual(len(self.store.accounts()), 1)
        self.assertEqual(self.store.draft("a", -1)["text"], "保留")

    async def test_remove_cancels_history_before_cleanup_and_blocks_new_sends(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def history(account_id):
            entered.set()
            await asyncio.Event().wait()

        async def remove_account(account_id):
            self.assertFalse(self.runtime.account_tasks)
            entered.set()
            await release.wait()

        self.service.history = history
        self.service.remove_account = remove_account
        history_task = asyncio.create_task(
            self.runtime.execute("history", "p", {"account_id": "a"})
        )
        await entered.wait()
        entered.clear()
        remove_task = asyncio.create_task(
            self.runtime.execute("remove_account", "d", {"account_id": "a"})
        )
        await entered.wait()
        await self.send(self.request())
        self.assertIn("正在修改", self.events[-1]["message"])
        self.assertFalse(self.service.calls)
        release.set()
        await remove_task
        self.assertTrue(history_task.cancelled())

    async def test_mock_logout_and_remove_survive_restart(self):
        mock = MockService(self.store, self.events.append)
        await mock.start()
        await mock.logout("mock-1")
        await mock.reconnect()
        with self.assertRaises(ValueError):
            await mock.groups("mock-1")
        for account in self.store.accounts():
            await mock.remove_account(account["id"])
        await mock.stop()
        restarted = MockService(self.store, self.events.append)
        await restarted.start()
        self.assertFalse(self.store.accounts())
        self.assertTrue(self.store.phrases())
        await restarted.stop()

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

    async def test_code_request_continues_after_telethon_switches_dc(self):
        from telethon.tl.types import auth

        from tg_chat_client.models import ProxyConfig

        self.store.save_connection(123, "test-only-hash", ProxyConfig(port=7892))
        service = TelegramService(self.store, self.events.append)
        with patch.dict("sys.modules", {"python_socks": Mock()}):
            client = service.client("login-test")
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.is_user_authorized = AsyncMock(return_value=False)
        client._switch_dc = AsyncMock()
        responses = [
            PhoneMigrateError(request=None, capture=5),
            auth.SentCode(type=auth.SentCodeTypeApp(length=5), phone_code_hash="test-code-hash"),
        ]

        def send(request, **kwargs):
            future = asyncio.get_running_loop().create_future()
            response = responses.pop(0)
            if isinstance(response, Exception):
                future.set_exception(response)
            else:
                future.set_result(response)
            return future

        client._sender.send = Mock(side_effect=send)
        try:
            with patch.object(service, "client", return_value=client):
                result = await service.request_code("+123")
            client._switch_dc.assert_awaited_once_with(5)
            self.assertEqual(client._sender.send.call_count, 2)
            self.assertEqual(service.codes[result["account_id"]]["hash"], "test-code-hash")
            self.assertEqual(self.events[-1]["status"], "等待验证码")
        finally:
            client.session.close()

    async def test_code_failure_is_not_retried_and_clears_pending_login(self):
        for error in (OSError("test network failure"), FloodWaitError(request=None, capture=30)):
            with self.subTest(error=type(error).__name__):
                service = TelegramService(self.store, self.events.append)
                client = Mock()
                client.connect = AsyncMock()
                client.disconnect = AsyncMock()
                client.send_code_request = AsyncMock(side_effect=error)
                with patch.object(service, "client", return_value=client):
                    with self.assertRaises(type(error)):
                        await service.request_code("+123")
                client.send_code_request.assert_awaited_once()
                client.disconnect.assert_awaited_once()
                self.assertFalse(service.codes)
                self.assertFalse(service.clients)
                self.assertFalse(self.store.accounts())

    async def test_code_dc_migrations_have_a_finite_limit(self):
        service = TelegramService(self.store, self.events.append)
        client = Mock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.send_code_request = AsyncMock(side_effect=PhoneMigrateError(request=None, capture=5))
        with patch.object(service, "client", return_value=client):
            with self.assertRaises(PhoneMigrateError):
                await service.request_code("+123")
        self.assertEqual(client.send_code_request.await_count, 3)
        self.assertFalse(service.codes)
        self.assertFalse(service.clients)

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
