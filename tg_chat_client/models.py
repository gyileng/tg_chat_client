from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")


def day_key(at: datetime | None = None) -> str:
    return (at or datetime.now(BEIJING)).astimezone(BEIJING).date().isoformat()


@dataclass(frozen=True)
class ProxyConfig:
    protocol: Literal["socks5", "http"] = "socks5"
    host: str = "127.0.0.1"
    port: int = 1080
    username: str = ""
    password: str = ""

    def validate(self) -> None:
        if self.protocol not in {"socks5", "http"}:
            raise ValueError("代理协议必须为 SOCKS5 或 HTTP")
        if not self.host.strip() or any(c.isspace() for c in self.host):
            raise ValueError("请填写代理主机名或 IP，不要包含协议或空格")
        if "://" in self.host or "/" in self.host:
            raise ValueError("代理地址仅填写主机名或 IP")
        if not 1 <= self.port <= 65535:
            raise ValueError("代理端口必须在 1–65535 之间")
        if self.password and not self.username:
            raise ValueError("填写代理密码时还需要填写用户名")

    def telethon(self) -> dict:
        self.validate()
        # Telethon delegates the tunnel to python-socks; there is no direct fallback.
        return {
            "proxy_type": self.protocol,
            "addr": self.host,
            "port": self.port,
            "rdns": True,
            "username": self.username or None,
            "password": self.password or None,
        }


@dataclass(frozen=True)
class SendRequest:
    request_id: str
    account_id: str
    chat_id: int
    text: str
    reply_to: int | None = None


@dataclass(frozen=True)
class ChatMessage:
    account_id: str
    chat_id: int
    message_id: int
    sender: str
    text: str
    time: str
    outgoing: bool = False
    reply_to: int | None = None
    media: str = ""
    sender_id: int | None = None
