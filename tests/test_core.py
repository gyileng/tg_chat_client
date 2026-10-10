import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tg_chat_client.models import ProxyConfig, SendRequest, day_key
from tg_chat_client.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.store.add_account("a", "账号 A", "+123")
        self.store.add_account("b", "账号 B", "+456")

    def tearDown(self):
        self.temp.cleanup()

    def request(self, request_id="r", account="a", chat=-1):
        return SendRequest(request_id, account, chat, "你好", 10)

    def test_success_deduplicates_request_and_remote_message(self):
        request = self.request()
        self.store.pending(request)
        self.assertTrue(self.store.success(request, 42, "2026-10-09"))
        self.assertFalse(self.store.success(request, 42, "2026-10-09"))
        duplicate = self.request("duplicate")
        self.store.pending(duplicate)
        self.assertFalse(self.store.success(duplicate, 42, "2026-10-09"))
        self.assertEqual(self.store.count("a", -1, "2026-10-09"), 1)

    def test_count_is_scoped_to_account_group_and_beijing_day(self):
        for index, (account, chat, day) in enumerate(
            [
                ("a", -1, "2026-10-09"),
                ("a", -2, "2026-10-09"),
                ("b", -1, "2026-10-09"),
                ("a", -1, "2026-10-10"),
            ]
        ):
            request = self.request(str(index), account, chat)
            self.store.pending(request)
            self.store.success(request, index, day)
        self.assertEqual(self.store.count("a", day="2026-10-09"), 2)
        self.assertEqual(self.store.count("a", -1, "2026-10-09"), 1)
        self.assertEqual(self.store.count("b", day="2026-10-09"), 1)
        self.assertEqual(self.store.count("a", -1, "2026-10-10"), 1)

    def test_failed_and_unknown_sends_do_not_count(self):
        for request_id, unknown in [("failed", False), ("unknown", True)]:
            self.store.pending(self.request(request_id))
            self.store.failed(request_id, unknown)
        self.assertEqual(self.store.count("a"), 0)
        with self.store.connect() as db:
            states = dict(db.execute("SELECT request_id,status FROM sends"))
        self.assertEqual(states, {"failed": "failed", "unknown": "unknown"})

    def test_restart_preserves_data_and_marks_interrupted_send_unknown(self):
        self.store.pending(self.request())
        self.store.replace_phrases(["短句一", "短句二"])
        self.store.save_draft("a", -1, "草稿", 12, "引用")
        restarted = Store(self.root)
        with restarted.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM sends").fetchone()[0], "unknown")
        self.assertEqual(restarted.draft("a", -1)["text"], "草稿")
        self.assertEqual([p["text"] for p in restarted.phrases()], ["短句一", "短句二"])
        self.assertEqual(restarted.count("a"), 0)

    def test_drafts_and_pins_are_isolated(self):
        self.store.save_draft("a", -1, "A 的草稿", 10, "A 引用")
        self.store.save_draft("b", -1, "B 的草稿", 11, "B 引用")
        self.store.save_draft("a", -2, "另一个群")
        self.store.toggle_pin("a", -1)
        self.assertEqual(self.store.draft("a", -1)["reply_id"], 10)
        self.assertEqual(self.store.draft("b", -1)["text"], "B 的草稿")
        self.assertEqual(self.store.draft("a", -2)["text"], "另一个群")
        self.assertEqual(self.store.pins("a"), {-1})
        self.assertEqual(self.store.pins("b"), set())

    def test_input_success_preserves_new_edits_and_new_quote(self):
        self.store.save_draft("a", -1, "发送之后的新草稿", 20, "新的引用")
        self.store.complete_draft(self.request(), "input")
        self.assertEqual(self.store.draft("a", -1)["text"], "发送之后的新草稿")
        self.assertEqual(self.store.draft("a", -1)["reply_id"], 20)

    def test_phrase_success_preserves_text_and_clears_submitted_quote(self):
        self.store.save_draft("a", -1, "正在编辑", 10, "引用")
        self.store.complete_draft(self.request(), "phrase")
        self.assertEqual(self.store.draft("a", -1)["text"], "正在编辑")
        self.assertIsNone(self.store.draft("a", -1)["reply_id"])

    def test_input_success_clears_only_original_target(self):
        self.store.save_draft("a", -1, "你好", 10, "引用")
        self.store.save_draft("b", -1, "另一个账号的草稿")
        self.store.complete_draft(self.request(), "input")
        self.assertEqual(self.store.draft("a", -1)["text"], "")
        self.assertEqual(self.store.draft("b", -1)["text"], "另一个账号的草稿")

    def test_future_database_version_is_not_overwritten(self):
        with self.store.connect() as db:
            db.execute("PRAGMA user_version=3")
        with self.assertRaises(RuntimeError):
            Store(self.root)
        with self.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_remove_clears_only_target_account_and_files(self):
        self.store.replace_phrases(["共享短句"])
        for aid in ("a", "b"):
            self.store.save_draft(aid, -1, "草稿")
            self.store.toggle_pin(aid, -1)
            request = self.request(aid, aid)
            self.store.pending(request)
            self.store.success(request, 1)
            (self.root / "sessions" / f"{aid}.session").write_text("fixture")
            folder = self.root / "cache" / aid
            folder.mkdir()
            (folder / "preview").write_text("fixture")
        self.store.remove_account("a")
        self.assertEqual([a["id"] for a in self.store.accounts()], ["b"])
        with self.store.connect() as db:
            for table in ("drafts", "pins", "sends", "counts"):
                self.assertEqual(
                    db.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id='a'").fetchone()[0],
                    0,
                )
        self.assertFalse((self.root / "sessions" / "a.session").exists())
        self.assertFalse((self.root / "cache" / "a").exists())
        self.assertTrue((self.root / "sessions" / "b.session").exists())
        self.assertEqual(self.store.count("b"), 1)
        self.assertEqual(self.store.phrases()[0]["text"], "共享短句")

    def test_cleanup_rejects_path_traversal(self):
        for aid in ("..", ".", "../a", "a/b", "a\\b"):
            with self.assertRaises(ValueError):
                self.store.clear_account_files(aid, cache=True)


class ConfigurationTests(unittest.TestCase):
    def test_beijing_midnight(self):
        self.assertEqual(day_key(datetime(2026, 10, 9, 15, 59, tzinfo=timezone.utc)), "2026-10-09")
        self.assertEqual(day_key(datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)), "2026-10-10")

    def test_proxy_protocols_and_remote_dns(self):
        for protocol in ("socks5", "http"):
            proxy = ProxyConfig(protocol, "localhost", 1080, "user", "pass")
            self.assertEqual(proxy.telethon()["proxy_type"], protocol)
            self.assertTrue(proxy.telethon()["rdns"])

    def test_invalid_proxy_configuration(self):
        for proxy in [
            ProxyConfig(host=""),
            ProxyConfig(host="http://localhost"),
            ProxyConfig(port=0),
            ProxyConfig(port=65536),
            ProxyConfig(password="secret"),
            ProxyConfig(protocol="invalid"),
        ]:
            with self.assertRaises(ValueError):
                proxy.validate()


def test_phrase_v1_migration_preserves_existing_data(tmp_path):
    import sqlite3

    path = tmp_path / "client.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE phrases (id INTEGER PRIMARY KEY, text TEXT NOT NULL, position INTEGER NOT NULL);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE sends (status TEXT);
        INSERT INTO phrases VALUES (7, '原有短句', 0);
        INSERT INTO settings VALUES ('other', 'true');
        PRAGMA user_version=1;
    """)
    db.close()
    store = Store(tmp_path)
    assert store.phrases() == [{"id": 7, "text": "原有短句", "position": 0, "alias": None}]
    assert store.phrase_prefix() == ";"
    assert store.setting("other") is True
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert Store(tmp_path).phrases() == store.phrases()


def test_phrase_alias_validation_is_atomic_and_persistent(tmp_path):
    import pytest

    store = Store(tmp_path)
    store.replace_phrases([{"text": "你好", "alias": "NH"}, "仅点击"], prefix=";+")
    before = store.phrases()
    for records, prefix in [
        ([{"text": "一", "alias": "nh"}, {"text": "二", "alias": "NH"}], ";"),
        ([{"text": "一", "alias": "不合法"}], ";"),
        ([{"text": "x" * 4097, "alias": "long"}], ";"),
        (["一"], ""),
        (["一"], "a"),
        (["一"], "; "),
    ]:
        with pytest.raises(ValueError):
            store.replace_phrases(records, prefix=prefix)
        assert store.phrases() == before
        assert store.phrase_prefix() == ";+"
    restarted = Store(tmp_path)
    assert restarted.phrases() == before
    assert restarted.phrase_prefix() == ";+"
    assert before[0]["alias"] == "nh"
    assert before[1]["alias"] is None
