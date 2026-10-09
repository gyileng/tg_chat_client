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
            db.execute("PRAGMA user_version=2")
        with self.assertRaises(RuntimeError):
            Store(self.root)
        with self.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)


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
