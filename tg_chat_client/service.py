import asyncio
import logging
import zlib
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from .models import ChatMessage, ProxyConfig, SendRequest
from .store import Store

logger = logging.getLogger(__name__)
BACKGROUND_DISCONNECT_SECONDS = 5 * 60


def flood_details(exc):
    """Classify only known RPCs; never infer an account-wide limit from a read."""
    from telethon.errors import SlowModeWaitError
    from telethon.tl import functions
    from telethon.tl.tlobject import TLRequest

    rpc = getattr(exc, "request", None)
    rpc_name = type(rpc).__name__ if isinstance(rpc, TLRequest) else "unknown"
    if isinstance(exc, SlowModeWaitError):
        return rpc_name, "slow_mode", "target", "群慢速模式"
    if isinstance(rpc, functions.messages.SendMessageRequest):
        return rpc_name, "send", "account", "Telegram 发送请求限流"
    if isinstance(
        rpc,
        (
            functions.channels.GetChannelsRequest,
            functions.messages.GetChatsRequest,
            functions.users.GetUsersRequest,
            functions.contacts.ResolveUsernameRequest,
        ),
    ):
        return rpc_name, "query", "query", "Telegram 获取群信息限流"
    if isinstance(
        rpc,
        (
            functions.messages.GetDialogsRequest,
            functions.messages.GetHistoryRequest,
            functions.messages.GetMessagesRequest,
            functions.channels.GetMessagesRequest,
        ),
    ):
        return rpc_name, "query", "query", "Telegram 查询限流"
    return rpc_name, "unknown", "account", "Telegram 要求等待（原因待确认）"


class MockService:
    def __init__(self, store: Store, emit):
        self.store, self.emit = store, emit
        self.messages = {}
        self.ids = 100
        self.ticker = None
        self.connected = False

    async def start(self):
        self.connected = True
        if not self.store.accounts() and not self.store.setting("mock_initialized", False):
            for i in range(1, 6):
                self.store.add_account(f"mock-{i}", f"模拟账号 {i}", f"mock-{i}")
            self.store.replace_phrases(["你好", "我也这样想", "谢谢分享", "收到"])
        self.store.set_setting("mock_initialized", True)
        for account in self.store.accounts():
            aid = account["id"]
            self.emit({"kind": "status", "account_id": aid, "status": self.mock_status(aid)})
            for chat_id in (-1001, -1002):
                if (aid, chat_id) in self.messages:
                    continue
                self.messages[aid, chat_id] = [
                    ChatMessage(
                        aid,
                        chat_id,
                        i,
                        "我" if i % 7 == 0 else f"模拟群友 {(i - 1) // 2 % 4 + 1}",
                        f"模拟消息 {i}",
                        "12:00",
                        outgoing=i % 7 == 0,
                        sender_id=100 + zlib.crc32(aid.encode())
                        if i % 7 == 0
                        else (i - 1) // 2 % 4 + 1,
                    )
                    for i in range(1, 31)
                ]
                self.messages[aid, chat_id][4] = replace(
                    self.messages[aid, chat_id][4], media="photo", text="图片占位示例"
                )
        if self.ticker is None:
            self.ticker = asyncio.create_task(self._tick())

    async def _tick(self):
        while True:
            await asyncio.sleep(8)
            for account in self.store.accounts():
                if self.mock_status(account["id"]) != "模拟连接":
                    continue
                self.ids += 1
                message = ChatMessage(
                    account["id"],
                    -1001,
                    self.ids,
                    f"模拟群友 {self.ids % 4 + 1}",
                    "新的模拟消息",
                    "12:01",
                    sender_id=self.ids % 4 + 1,
                )
                self.messages[account["id"], -1001].append(message)
                self.emit({"kind": "message", "message": message})

    async def stop(self):
        self.connected = False
        if self.ticker:
            self.ticker.cancel()
            await asyncio.gather(self.ticker, return_exceptions=True)
            self.ticker = None

    async def groups(self, account_id):
        self.check_account(account_id)
        return [{"id": -1001, "title": "主要聊天群"}, {"id": -1002, "title": "其他聊天群"}]

    async def history(self, account_id, chat_id, before=0):
        self.check_account(account_id)
        await asyncio.sleep(0.05)
        messages = self.messages.get((account_id, chat_id), [])
        return [m for m in messages if not before or m.message_id < before][-50:]

    async def send(self, request: SendRequest):
        self.check_account(request.account_id)
        await asyncio.sleep(0.15)
        self.ids += 1
        message = ChatMessage(
            request.account_id,
            request.chat_id,
            self.ids,
            "我",
            request.text,
            datetime.now().strftime("%H:%M"),
            True,
            request.reply_to,
            sender_id=100 + zlib.crc32(request.account_id.encode()),
            reply_text=request.reply_text,
        )
        self.messages[request.account_id, request.chat_id].append(message)
        self.emit({"kind": "message", "message": message})
        return message

    async def reconnect(self):
        await self.stop()
        await self.start()

    def mock_status(self, aid):
        if aid in self.store.setting("mock_logged_out", []):
            return "需要登录"
        return "模拟连接" if self.connected else "后台已断开"

    def check_account(self, aid):
        if (
            not any(a["id"] == aid for a in self.store.accounts())
            or self.mock_status(aid) != "模拟连接"
        ):
            raise ValueError("账号未连接或未登录")

    async def logout(self, account_id):
        logged_out = set(self.store.setting("mock_logged_out", []))
        logged_out.add(account_id)
        self.store.set_setting("mock_logged_out", sorted(logged_out))
        self.emit({"kind": "status", "account_id": account_id, "status": "需要登录"})

    async def remove_account(self, account_id):
        self.store.remove_account(account_id)
        self.store.set_setting(
            "mock_logged_out",
            [aid for aid in self.store.setting("mock_logged_out", []) if aid != account_id],
        )
        for target in list(self.messages):
            if target[0] == account_id:
                self.messages.pop(target)


class TelegramService:
    def __init__(self, store: Store, emit):
        self.store, self.emit = store, emit
        self.clients = {}
        self.codes = {}
        self.input_peers = {}
        self.monitor = None

    def client(self, account_id):
        from telethon import TelegramClient

        settings = self.store.export_settings()
        if not settings:
            raise ValueError("请先配置 API ID、API Hash 和代理")
        try:
            import python_socks  # noqa: F401
        except ImportError as exc:
            raise ValueError("缺少 python-socks 代理依赖，请重新安装依赖或使用完整便携包") from exc
        proxy = ProxyConfig(**settings["proxy"])
        return TelegramClient(
            str(self.store.root / "sessions" / account_id),
            settings["api_id"],
            settings["api_hash"],
            proxy=proxy.telethon(),
            request_retries=0,
            raise_last_call_error=True,
            connection_retries=2,
            retry_delay=1,
            flood_sleep_threshold=0,
            auto_reconnect=True,
        )

    def status(self, aid, state):
        self.emit({"kind": "status", "account_id": aid, "status": state})

    def install_handler(self, aid, client):
        from telethon import events

        async def received(event):
            if event.is_group:
                self.emit({"kind": "message", "message": await self.convert(aid, event.message)})

        client.add_event_handler(received, events.NewMessage())

    async def start(self):
        await asyncio.gather(*(self.connect(a["id"]) for a in self.store.accounts()))
        if self.monitor is None:
            self.monitor = asyncio.create_task(self._monitor())

    async def _monitor(self):
        while True:
            await asyncio.sleep(5)
            for aid, client in list(self.clients.items()):
                if aid not in self.codes:
                    self.status(aid, "已连接" if client.is_connected() else "连接已断开")

    async def connect(self, aid):
        if not (self.store.root / "sessions" / (aid + ".session")).exists():
            self.status(aid, "需要登录")
            return
        self.status(aid, "连接中")
        client = None
        try:
            client = self.client(aid)
            await asyncio.wait_for(client.connect(), 20)
            if not await client.is_user_authorized():
                self.status(aid, "需要登录")
                await client.disconnect()
                return
            self.install_handler(aid, client)
            self.clients[aid] = client
            self.status(aid, "已连接")
        except BaseException as exc:
            self.status(aid, "连接失败：请检查配置/代理")
            if client:
                await client.disconnect()
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def stop(self):
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None
        await asyncio.gather(
            *(c.disconnect() for c in self.clients.values()), return_exceptions=True
        )
        self.clients.clear()
        self.codes.clear()
        self.input_peers.clear()

    async def reconnect(self):
        await self.stop()
        await self.start()

    def authorized(self, aid):
        client = self.clients.get(aid)
        if not client or aid in self.codes or not client.is_connected():
            raise ValueError("账号未连接或未登录，请检查代理并重新连接")
        return client

    async def invalidate(self, aid):
        client = self.clients.pop(aid, None)
        self.codes.pop(aid, None)
        self.input_peers.pop(aid, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        self.status(aid, "登录已失效：请重新登录")

    async def request_code(self, phone):
        from telethon.errors import NetworkMigrateError, PhoneMigrateError, UserMigrateError

        phone = phone.strip()
        if not phone.startswith("+") or not phone[1:].isdigit():
            raise ValueError("手机号请使用 +国家代码号码 格式")
        account = next((a for a in self.store.accounts() if a["phone"] == phone), None)
        aid = account["id"] if account else uuid4().hex
        self.input_peers.pop(aid, None)
        if aid in self.clients:
            await self.clients.pop(aid).disconnect()
        client = self.client(aid)
        self.clients[aid] = client
        self.codes[aid] = {"phone": phone}
        try:
            await asyncio.wait_for(client.connect(), 20)
            for attempt in range(3):
                try:
                    code = await client.send_code_request(phone)
                    break
                except (PhoneMigrateError, NetworkMigrateError, UserMigrateError):
                    # Telethon has already switched DC. Continue only this rejected
                    # login request; never retry network failures or message sends.
                    if attempt == 2:
                        raise
            self.codes[aid]["hash"] = code.phone_code_hash
            self.store.add_account(aid, account["label"] if account else phone, phone)
            self.status(aid, "等待验证码")
            return {"account_id": aid}
        except BaseException:
            self.codes.pop(aid, None)
            self.clients.pop(aid, None)
            await client.disconnect()
            raise

    async def sign_in(self, account_id, code="", password=""):
        from telethon.errors import SessionPasswordNeededError
        from telethon.utils import get_display_name

        client = self.clients.get(account_id)
        pending = self.codes.get(account_id)
        if not client or not pending:
            raise ValueError("登录已过期，请重新获取验证码")
        try:
            if password:
                await client.sign_in(password=password)
            else:
                await client.sign_in(pending["phone"], code, phone_code_hash=pending["hash"])
        except SessionPasswordNeededError:
            return {"account_id": account_id, "password_needed": True}
        me = await client.get_me()
        self.store.add_account(
            account_id, get_display_name(me) or pending["phone"], pending["phone"]
        )
        self.codes.pop(account_id, None)
        self.install_handler(account_id, client)
        self.status(account_id, "已连接")
        return {"account_id": account_id, "password_needed": False}

    async def cancel_login(self, account_id):
        if account_id in self.codes:
            self.codes.pop(account_id, None)
            client = self.clients.pop(account_id, None)
            if client:
                await client.disconnect()
            self.status(account_id, "需要登录")

    async def logout(self, account_id):
        if not any(a["id"] == account_id for a in self.store.accounts()):
            raise ValueError("账号不存在")
        client = self.clients.pop(account_id, None)
        self.codes.pop(account_id, None)
        self.input_peers.pop(account_id, None)
        if client is None and (self.store.root / "sessions" / (account_id + ".session")).exists():
            client = self.client(account_id)
        try:
            if client:
                if not client.is_connected():
                    await client.connect()
                if await client.is_user_authorized() and not await client.log_out():
                    raise ValueError("退出登录未成功，请检查连接后重试")
        except BaseException:
            self.status(account_id, "退出未完成：请重新连接后重试")
            raise
        finally:
            if client:
                await client.disconnect()
                if client.session:
                    client.session.close()
        self.store.clear_account_files(account_id)
        self.status(account_id, "需要登录")

    async def remove_account(self, account_id):
        # Keep all local records if server-side logout fails.
        await self.logout(account_id)
        self.store.remove_account(account_id)

    async def groups(self, account_id):
        result = []
        async for dialog in self.authorized(account_id).iter_dialogs():
            if dialog.is_group:
                self.input_peers.setdefault(account_id, {})[dialog.id] = dialog.input_entity
                result.append({"id": dialog.id, "title": dialog.name})
        return result

    async def convert(self, aid, message, *, resolve_reply=True):
        from telethon.utils import get_display_name

        try:
            sender = await message.get_sender()
        except Exception:
            # A name lookup is optional; still show the actual message if it fails.
            sender = None
        name = get_display_name(sender) or getattr(sender, "username", None) or "未知发送者"
        media = ""
        if message.photo:
            media = "photo"
        elif message.sticker:
            media = "sticker"
        elif message.video or message.gif:
            media = "video"
        elif message.voice or message.audio:
            media = "audio"
        elif message.document:
            media = "file"
        elif message.media:
            media = "other"
        reply_text = ""
        if message.reply_to_msg_id and resolve_reply:
            try:
                original = await asyncio.wait_for(message.get_reply_message(), 5)
                if original:
                    reply_text = (await self.convert(aid, original, resolve_reply=False)).quote_text
            except Exception:
                # A missing quote must not hide the message or fail history loading.
                pass
            if getattr(getattr(message, "reply_to", None), "reply_to_peer_id", None):
                reply_text = reply_text or "原消息不可用"
        return ChatMessage(
            aid,
            message.chat_id,
            message.id,
            name,
            message.raw_text or "",
            message.date.astimezone().strftime("%m-%d %H:%M"),
            bool(message.out),
            message.reply_to_msg_id,
            media,
            sender_id=getattr(message, "sender_id", None),
            reply_text=reply_text,
        )

    async def history(self, account_id, chat_id, before=0):
        client = self.authorized(account_id)
        messages = await client.get_messages(chat_id, limit=50, offset_id=before)
        external = {
            m.id
            for m in messages
            if getattr(getattr(m, "reply_to", None), "reply_to_peer_id", None)
        }
        converted = [
            await self.convert(account_id, m, resolve_reply=m.id in external) for m in messages
        ]
        originals = {m.message_id: m for m in converted}
        missing = sorted(
            {m.reply_to for m in converted if m.reply_to and m.message_id not in external}
            - originals.keys()
        )
        if missing:
            try:
                extra = await asyncio.wait_for(client.get_messages(chat_id, ids=missing), 5)
                for message in extra:
                    if message:
                        original = await self.convert(account_id, message, resolve_reply=False)
                        originals[original.message_id] = original
            except Exception:
                pass
        return [
            replace(m, reply_text=originals[m.reply_to].quote_text)
            if m.reply_to in originals and m.message_id not in external
            else m
            for m in reversed(converted)
        ]

    async def send(self, request: SendRequest):
        client = self.authorized(request.account_id)
        peers = self.input_peers.setdefault(request.account_id, {})
        peer = peers.get(request.chat_id)
        if peer is None:
            # Telethon retains per-RPC FloodWait deadlines for unresolved queries.
            # Do not retry a failed lookup or manufacture an access hash.
            peer = await client.get_input_entity(request.chat_id)
            peers[request.chat_id] = peer
        message = await client.send_message(
            peer,
            request.text,
            reply_to=request.reply_to,
            parse_mode=None,
            link_preview=False,
        )
        # No secondary network request after a confirmed send: sender lookup failure
        # must not turn a successful send into an "unknown" result.
        return ChatMessage(
            request.account_id,
            request.chat_id,
            message.id,
            "我",
            request.text,
            message.date.astimezone().strftime("%m-%d %H:%M"),
            True,
            request.reply_to,
            sender_id=getattr(message, "sender_id", None),
            reply_text=request.reply_text,
        )


class ServiceRuntime:
    """Async request execution and send accounting, independent of the GUI."""

    def __init__(self, store, service, emit):
        self.store = store
        self.service = service
        self.emit = emit
        self.busy = set()
        self.control_lock = asyncio.Lock()
        self.blocked_until = {}
        self.blocked_accounts = {}
        self.account_tasks = {}
        self.control_tasks = {}
        self.sends_idle = asyncio.Event()
        self.sends_idle.set()
        self.foreground = True
        self.background_since = None
        self.background_delay = BACKGROUND_DISCONNECT_SECONDS
        self.connection_state = "online"
        self.visibility_changed = asyncio.Event()
        self.connection_task = None

    def set_foreground(self, foreground):
        if self.foreground == foreground:
            return
        self.foreground = foreground
        self.background_since = None if foreground else asyncio.get_running_loop().time()
        self.visibility_changed.set()
        if self.connection_task is None:
            self.connection_task = asyncio.create_task(self._manage_connection())

    def connection_status(self, state):
        self.connection_state = state
        self.emit({"kind": "connection_state", "state": state})

    async def _manage_connection(self):
        while True:
            self.visibility_changed.clear()
            if self.foreground and self.connection_state == "paused":
                self.connection_status("resuming")
                try:
                    async with self.control_lock:
                        await asyncio.wait_for(self.service.start(), 60)
                except Exception as exc:
                    # A partial start must not leave clients outside lifecycle management.
                    await self.service.stop()
                    logger.warning("operation=resume error=%s", type(exc).__name__)
                    self.connection_state = "online"
                    self.emit({"kind": "connection_state", "state": "resume_failed"})
                else:
                    self.connection_status("online")
                continue
            if not self.foreground and self.connection_state == "online":
                delay = (
                    self.background_since
                    + self.background_delay
                    - asyncio.get_running_loop().time()
                )
                if delay > 0:
                    try:
                        await asyncio.wait_for(self.visibility_changed.wait(), delay)
                        continue
                    except TimeoutError:
                        pass
                self.connection_status("pausing")
                # Cancel network reads and login/connect requests, but let accepted sends
                # finish under their existing timeout. Logout/removal must finish atomically.
                pending = list(self.account_tasks) + [
                    task
                    for task, operation in self.control_tasks.items()
                    if operation not in {"logout", "remove_account"}
                ]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                await self.sends_idle.wait()
                async with self.control_lock:
                    await self.service.stop()
                self.connection_status("paused")
                continue
            await self.visibility_changed.wait()

    async def execute(self, operation, request_id, args):
        # The service thread sets service before it starts the loop.
        envelope = {"operation": operation, "request_id": request_id, **args}
        request = args.get("request")
        target = (request.account_id, request.chat_id) if request else None
        admitted = False
        account_id = request.account_id if request else args.get("account_id")
        task = asyncio.current_task()
        try:
            if operation == "set_foreground":
                self.set_foreground(args["foreground"])
                return
            if self.connection_state != "online":
                raise ValueError("后台连接已暂停或正在恢复，请等待连接完成后再操作")
            if operation in {"send", "groups", "history"} and self.control_lock.locked():
                raise ValueError("正在修改登录或连接，请稍后操作")
            if operation in {"groups", "history"}:
                self.account_tasks[task] = account_id
            if operation == "send":
                deadline = max(
                    self.blocked_until.get(target, 0),
                    self.blocked_accounts.get(request.account_id, 0),
                )
                if deadline > asyncio.get_running_loop().time():
                    raise ValueError("Telegram 等待时间尚未结束，请稍后手动发送")
                if not request.text.strip() or len(request.text) > 4096:
                    raise ValueError("消息须为 1–4096 个字符")
                self.busy.add(request.request_id)
                self.sends_idle.clear()
                admitted = True
                self.store.pending(request)
                result = await asyncio.wait_for(self.service.send(request), 30)
                self.store.success(request, result.message_id)
            elif operation in {
                "start",
                "reconnect",
                "request_code",
                "sign_in",
                "cancel_login",
                "logout",
                "remove_account",
            }:
                self.control_tasks[task] = operation
                async with self.control_lock:
                    if self.connection_state != "online":
                        raise ValueError("后台连接已暂停或正在恢复，请等待连接完成后再操作")
                    if self.busy:
                        raise ValueError("请等待发送结束后再修改登录或连接")
                    if operation in {"logout", "remove_account"}:
                        pending = [t for t, aid in self.account_tasks.items() if aid == account_id]
                        for pending_task in pending:
                            pending_task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                    result = await asyncio.wait_for(getattr(self.service, operation)(**args), 60)
            else:
                result = await asyncio.wait_for(getattr(self.service, operation)(**args), 60)
            self.emit({"kind": "result", **envelope, "result": result})
        except Exception as exc:
            from telethon.errors import (
                FloodWaitError,
                RPCError,
                SlowModeWaitError,
                UnauthorizedError,
            )

            wait = exc.seconds if isinstance(exc, (FloodWaitError, SlowModeWaitError)) else 0
            rpc_name, wait_kind, wait_scope, wait_label = flood_details(exc)
            if wait and target:
                if wait_scope == "account":
                    self.blocked_accounts[request.account_id] = (
                        asyncio.get_running_loop().time() + wait
                    )
                elif wait_scope == "target":
                    self.blocked_until[target] = asyncio.get_running_loop().time() + wait
            unknown = admitted and not isinstance(exc, (ValueError, RPCError))
            account_id = request.account_id if request else args.get("account_id")
            if (
                isinstance(exc, UnauthorizedError)
                and account_id
                and hasattr(self.service, "invalidate")
            ):
                await self.service.invalidate(account_id)
            if admitted:
                self.store.failed(request.request_id, unknown=unknown)
            if wait:
                message = f"{wait_label}，需等待 {wait} 秒，请稍后手动重试"
            elif isinstance(exc, ValueError):
                message = str(exc)
            elif unknown:
                message = "发送结果不确定，请先查看群消息，不要直接重复发送"
            elif isinstance(exc, RPCError):
                reasons = {
                    "PhoneCodeInvalidError": "验证码不正确",
                    "PhoneCodeExpiredError": "验证码已过期，请重新获取",
                    "PasswordHashInvalidError": "两步验证密码不正确",
                    "ChatWriteForbiddenError": "当前账号没有在这个群发言的权限",
                    "UserBannedInChannelError": "当前账号在这个群被禁止发言",
                    "AuthKeyUnregisteredError": "登录已失效，请重新登录",
                }
                message = reasons.get(
                    type(exc).__name__, f"Telegram 拒绝操作：{type(exc).__name__}"
                )
            else:
                message = f"操作失败：{type(exc).__name__}；请检查代理、连接或登录状态"
            # Class names and local IDs only: RPC repr/str may expose text or credentials.
            logger.warning(
                "operation=%s error=%s rpc_request=%s wait_seconds=%s request_id=%s",
                operation,
                type(exc).__name__,
                rpc_name,
                wait,
                request_id,
            )
            self.emit(
                {
                    "kind": "error",
                    **envelope,
                    "message": message,
                    "wait": wait,
                    "wait_scope": wait_scope,
                    "wait_kind": wait_kind,
                    "rpc_request": rpc_name,
                }
            )
        except asyncio.CancelledError:
            if admitted:
                self.store.failed(request.request_id, unknown=True)
            raise
        finally:
            self.account_tasks.pop(task, None)
            self.control_tasks.pop(task, None)
            if admitted:
                self.busy.discard(request.request_id)
                if not self.busy:
                    self.sends_idle.set()
