import json
import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .models import SendRequest, day_key


class Store:
    """Short-lived connections permit use from the UI and the service thread."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            root.chmod(0o700)
        for folder in ("sessions", "cache", "logs"):
            (root / folder).mkdir(exist_ok=True)
        self.path = root / "client.sqlite3"
        self._migrate()
        with self.connect() as db:
            db.execute("UPDATE sends SET status='unknown' WHERE status='pending'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _migrate(self):
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("数据来自较新的程序版本，请使用新版程序")
            if version == 0:
                db.executescript("""
                    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE accounts (
                        id TEXT PRIMARY KEY, label TEXT NOT NULL, phone TEXT NOT NULL,
                        last_chat INTEGER
                    );
                    CREATE TABLE phrases (
                        id INTEGER PRIMARY KEY, text TEXT NOT NULL, position INTEGER NOT NULL
                    );
                    CREATE TABLE drafts (
                        account_id TEXT, chat_id INTEGER, text TEXT NOT NULL,
                        reply_id INTEGER, reply_text TEXT,
                        PRIMARY KEY(account_id, chat_id)
                    );
                    CREATE TABLE pins (
                        account_id TEXT, chat_id INTEGER, PRIMARY KEY(account_id, chat_id)
                    );
                    CREATE TABLE sends (
                        request_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                        chat_id INTEGER NOT NULL, status TEXT NOT NULL,
                        message_id INTEGER, day TEXT,
                        UNIQUE(account_id, chat_id, message_id)
                    );
                    CREATE TABLE counts (
                        account_id TEXT, chat_id INTEGER, day TEXT, total INTEGER NOT NULL,
                        PRIMARY KEY(account_id, chat_id, day)
                    );
                    PRAGMA user_version=1;
                """)

            if version < 2:
                db.execute("BEGIN")
                db.execute("ALTER TABLE phrases ADD COLUMN alias TEXT")
                db.execute("CREATE UNIQUE INDEX phrases_alias ON phrases(alias)")
                db.execute("PRAGMA user_version=2")

    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    def accounts(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM accounts ORDER BY rowid")]

    def add_account(self, account_id, label, phone):
        with self.connect() as db:
            db.execute(
                "INSERT INTO accounts(id,label,phone) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET label=excluded.label,phone=excluded.phone",
                (account_id, label, phone),
            )

    def last_chat(self, account_id, chat_id):
        with self.connect() as db:
            db.execute("UPDATE accounts SET last_chat=? WHERE id=?", (chat_id, account_id))

    def clear_account_files(self, account_id, cache=False):
        # IDs originate in the account table, but never allow paths outside the data directory.
        if (
            not account_id
            or account_id in {".", ".."}
            or Path(account_id).name != account_id
            or "/" in account_id
            or "\\" in account_id
        ):
            raise ValueError("账号标识无效")
        for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
            (self.root / "sessions" / (account_id + suffix)).unlink(missing_ok=True)
        folder = self.root / "cache" / account_id
        if cache and folder.exists():
            shutil.rmtree(folder)

    def remove_account(self, account_id):
        self.clear_account_files(account_id, cache=True)
        with self.connect() as db:
            for table in ("drafts", "pins", "sends", "counts"):
                db.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,))
            db.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def draft(self, account_id, chat_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT text,reply_id,reply_text FROM drafts WHERE account_id=? AND chat_id=?",
                (account_id, chat_id),
            ).fetchone()
        return dict(row) if row else {"text": "", "reply_id": None, "reply_text": ""}

    def save_draft(self, account_id, chat_id, text, reply_id=None, reply_text=""):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO drafts VALUES(?,?,?,?,?)",
                (account_id, chat_id, text, reply_id, reply_text),
            )

    def complete_draft(self, request: SendRequest, source: str):
        """Don't erase edits typed while the frozen request was in flight."""
        draft = self.draft(request.account_id, request.chat_id)
        text = draft["text"]
        if source == "input" and text == request.text:
            text = ""
        reply_id, reply_text = draft["reply_id"], draft["reply_text"]
        if reply_id == request.reply_to:
            reply_id, reply_text = None, ""
        self.save_draft(request.account_id, request.chat_id, text, reply_id, reply_text)

    def phrases(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM phrases ORDER BY position,id")]

    def phrase_prefix(self):
        return self.setting("phrase_prefix", ";")

    def replace_phrases(self, texts, prefix=None):
        """Accept legacy text lists or records containing text and an optional alias."""
        if prefix is not None and (
            not prefix or any(c.isspace() or c.isalnum() or c == "_" for c in prefix)
        ):
            raise ValueError("前缀须为一个或多个非空白符号")
        rows, aliases = [], set()
        for item in texts:
            text = (item if isinstance(item, str) else item["text"]).strip()
            alias = "" if isinstance(item, str) else (item.get("alias") or "").strip().lower()
            if alias and not re.fullmatch(r"[a-z0-9_]+", alias):
                raise ValueError("缩写仅支持英文字母、数字和下划线")
            if alias and alias in aliases:
                raise ValueError(f"缩写重复：{alias}")
            if not text:
                continue
            if len(text) > 4096:
                raise ValueError("每条短句最多 4096 字符")
            if alias:
                aliases.add(alias)
            rows.append((text, len(rows), alias or None))
        with self.connect() as db:
            db.execute("DELETE FROM phrases")
            db.executemany("INSERT INTO phrases(text,position,alias) VALUES(?,?,?)", rows)
            if prefix is not None:
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES (?, ?)",
                    ("phrase_prefix", json.dumps(prefix)),
                )

    def pins(self, account_id):
        with self.connect() as db:
            return {
                r[0]
                for r in db.execute("SELECT chat_id FROM pins WHERE account_id=?", (account_id,))
            }

    def toggle_pin(self, account_id, chat_id):
        with self.connect() as db:
            existing = db.execute(
                "SELECT 1 FROM pins WHERE account_id=? AND chat_id=?", (account_id, chat_id)
            ).fetchone()
            if existing:
                db.execute(
                    "DELETE FROM pins WHERE account_id=? AND chat_id=?", (account_id, chat_id)
                )
            else:
                db.execute("INSERT INTO pins VALUES(?,?)", (account_id, chat_id))

    def pending(self, request: SendRequest):
        with self.connect() as db:
            db.execute(
                "INSERT INTO sends(request_id,account_id,chat_id,status) VALUES(?,?,?,'pending')",
                (request.request_id, request.account_id, request.chat_id),
            )

    def failed(self, request_id, unknown=False):
        with self.connect() as db:
            db.execute(
                "UPDATE sends SET status=? WHERE request_id=? AND status='pending'",
                ("unknown" if unknown else "failed", request_id),
            )

    def success(self, request: SendRequest, message_id: int, day: str | None = None) -> bool:
        day = day or day_key()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM sends WHERE request_id=?", (request.request_id,)
            ).fetchone()
            if not row or row[0] == "success":
                return False
            duplicate = db.execute(
                "SELECT 1 FROM sends WHERE account_id=? AND chat_id=? AND message_id=?",
                (request.account_id, request.chat_id, message_id),
            ).fetchone()
            if duplicate:
                db.execute(
                    "UPDATE sends SET status='duplicate' WHERE request_id=?", (request.request_id,)
                )
                return False
            db.execute(
                "UPDATE sends SET status='success',message_id=?,day=? WHERE request_id=?",
                (message_id, day, request.request_id),
            )
            db.execute(
                "INSERT INTO counts VALUES(?,?,?,1) ON CONFLICT(account_id,chat_id,day) "
                "DO UPDATE SET total=total+1",
                (request.account_id, request.chat_id, day),
            )
        return True

    def count(self, account_id, chat_id=None, day=None):
        query = "SELECT COALESCE(SUM(total),0) FROM counts WHERE account_id=? AND day=?"
        args = [account_id, day or day_key()]
        if chat_id is not None:
            query += " AND chat_id=?"
            args.append(chat_id)
        with self.connect() as db:
            return db.execute(query, args).fetchone()[0]

    def export_settings(self):
        # For local settings UI only, never diagnostics or logs.
        return self.setting("connection", {})

    def save_connection(self, api_id, api_hash, proxy):
        proxy.validate()
        if api_id <= 0 or not api_hash.strip():
            raise ValueError("请填写有效的 API ID 和 API Hash")
        self.set_setting(
            "connection", {"api_id": api_id, "api_hash": api_hash, "proxy": asdict(proxy)}
        )
