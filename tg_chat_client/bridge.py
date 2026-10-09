import asyncio
import logging
import threading
from uuid import uuid4

from PySide6.QtCore import QObject, Signal

from .service import MockService, ServiceRuntime, TelegramService
from .store import Store

logger = logging.getLogger(__name__)


class ServiceBridge(QObject):
    event = Signal(object)

    def __init__(self, store: Store, mock=False):
        super().__init__()
        self.store = store
        self.mock = mock
        self.loop = asyncio.new_event_loop()
        self.service = None
        self.runtime = None
        self.closed = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="telegram-service")
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        service_type = MockService if self.mock else TelegramService
        self.service = service_type(self.store, self.event.emit)
        self.runtime = ServiceRuntime(self.store, self.service, self.event.emit)
        self.loop.run_forever()
        self.loop.close()

    def submit(self, operation, **args):
        if self.closed:
            raise RuntimeError("服务已经关闭")
        request_id = uuid4().hex
        asyncio.run_coroutine_threadsafe(self._dispatch(operation, request_id, args), self.loop)
        return request_id

    async def _dispatch(self, operation, request_id, args):
        await self.runtime.execute(operation, request_id, args)

    def close(self):
        if self.closed:
            return
        self.closed = True

        async def shutdown():
            current = asyncio.current_task()
            pending = [t for t in asyncio.all_tasks() if t is not current]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await self.service.stop()

        future = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        try:
            future.result(timeout=10)
        except Exception:
            logger.warning("service shutdown did not finish in time")
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
