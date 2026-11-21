import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rssplus_test_module", ROOT / "src/rssplus.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PolicyTests(unittest.TestCase):
    def test_tracking_removed_from_key(self):
        a = SimpleNamespace(link="https://example.com/story?utm_source=x&id=5", title="A", html="B")
        b = SimpleNamespace(link="http://EXAMPLE.COM/story?id=5&utm_campaign=y", title="Other", html="X")
        self.assertEqual(module.fingerprint(a), module.fingerprint(b))

    def test_text_fallback(self):
        a = SimpleNamespace(link=None, title="خبر", html="<b>جزئیات</b>")
        b = SimpleNamespace(link="", title="خبر", html="جزئیات")
        self.assertEqual(module.fingerprint(a), module.fingerprint(b))

    def test_include_exclude(self):
        p = SimpleNamespace(title="Neuroscience", html="<b>Research</b>")
        self.assertFalse(module.is_filtered(p, {"include": ["science"], "exclude": []}))
        self.assertTrue(module.is_filtered(p, {"include": [], "exclude": ["research"]}))
        self.assertTrue(module.is_filtered(p, {"include": ["politics"], "exclude": []}))

    def test_channel_overrides(self):
        c = {"default": {"mode": "auto", "deduplicate": True},
             "targets": {"-1001": {"mode": "review"}}}
        p = module.policy_for(c, -1001)
        self.assertEqual(p["mode"], "review")
        self.assertTrue(p["deduplicate"])

    def test_duplicate_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(module, "_db_path", return_value=Path(tmp) / "data.sqlite3"):
                key, new = module._record(-1001, "f" * 64, 99, json.dumps({"entry": {}}), "pending")
                self.assertTrue(new)
                _, again = module._record(-1001, "f" * 64, 99, "{}", "pending")
                self.assertFalse(again)
                self.assertTrue(module._transition(key, "pending", "published"))
                self.assertFalse(module._transition(key, "pending", "published"))


class PublishTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_publish_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            post = SimpleNamespace(link="https://example.com/a", title="One", html="Body")
            feed = SimpleNamespace(title="Feed", link="https://example.com/rss")
            sub = SimpleNamespace(id=7, user_id=-1001)
            sender = AsyncMock()
            with patch.object(module, "load_config", return_value={
                     "enabled": True, "default": {"mode": "auto", "deduplicate": True}}), \
                 patch.object(module, "_db_path", return_value=Path(tmp) / "db.sqlite3"), \
                 patch.object(module, "_webhook", new_callable=AsyncMock):
                await module.process_post(sub=sub, post=post, entry={"title": "One"},
                                          feed=feed, send_func=sender)
                await module.process_post(sub=sub, post=post, entry={"title": "One"},
                                          feed=feed, send_func=sender)
                self.assertEqual(sender.await_count, 1)

    async def test_review_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            post = SimpleNamespace(link="https://example.com/b", title="Two", html="News")
            feed = SimpleNamespace(title="Feed", link="https://example.com/rss")
            sub = SimpleNamespace(id=8, user_id=-1002)
            sender, preview = AsyncMock(), AsyncMock()
            with patch.object(module, "load_config", return_value={
                     "enabled": True, "review_chat_id": 1234,
                     "default": {"mode": "auto", "deduplicate": True},
                     "targets": {"-1002": {"mode": "review"}}}), \
                 patch.object(module, "_db_path", return_value=Path(tmp) / "db.sqlite3"), \
                 patch.object(module, "_send_review", new=preview):
                await module.process_post(sub=sub, post=post, entry={"title": "Two"},
                                          feed=feed, send_func=sender)
                sender.assert_not_awaited()
                preview.assert_awaited_once()
                self.assertEqual(module._pending_items()[0]["status"], "pending")


class ReviewActionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_reject_only_pending_item_of_authorized_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"enabled": True, "review_chat_id": 123}
            with patch.object(module, "load_config", return_value=config), \
                 patch.object(module, "_db_path", return_value=Path(tmp) / "reviews.sqlite3"):
                key, _ = module._record(-1001, "a"*64, 10, json.dumps({
                    "feed_title": "Test", "feed_link": "https://example.org/rss",
                    "entry": {"title": "Review"}
                }), "pending")
                with self.assertRaises(ValueError):
                    await module.act_on_review(key, -1002, "reject")
                self.assertEqual(module._get_item(key)["status"], "pending")
                self.assertEqual(await module.act_on_review(key, -1001, "reject"), "rejected")
                self.assertEqual(module._get_item(key)["status"], "rejected")
                with self.assertRaises(ValueError):
                    await module.act_on_review(key, -1001, "reject")

    async def test_approval_sends_once_and_commits_published_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"enabled": True, "review_chat_id": 123}
            with patch.object(module, "load_config", return_value=config), \
                 patch.object(module, "_db_path", return_value=Path(tmp) / "reviews.sqlite3"), \
                 patch.object(module, "_publish_review_item",
                              new_callable=AsyncMock) as sender, \
                 patch.object(module, "_webhook", new_callable=AsyncMock) as webhook:
                key, _ = module._record(-1001, "b"*64, 10, "{}","pending")
                self.assertEqual(await module.act_on_review(key, -1001, "approve"), "published")
                self.assertEqual(module._get_item(key)["status"], "published")
                sender.assert_awaited_once()
                webhook.assert_awaited_once()
                with self.assertRaises(ValueError):
                    await module.act_on_review(key, -1001, "approve")
                self.assertEqual(sender.await_count, 1)

    async def test_failed_delivery_returns_item_to_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"enabled": True, "review_chat_id": 123}
            with patch.object(module, "load_config", return_value=config), \
                 patch.object(module, "_db_path", return_value=Path(tmp) / "reviews.sqlite3"), \
                 patch.object(module, "_publish_review_item",
                              new_callable=AsyncMock) as sender:
                sender.side_effect = RuntimeError("synthetic send failure")
                key, _ = module._record(-1001, "c"*64, 10, "{}","pending")
                with self.assertRaises(RuntimeError):
                    await module.act_on_review(key, -1001, "approve")
                self.assertEqual(module._get_item(key)["status"], "pending")

