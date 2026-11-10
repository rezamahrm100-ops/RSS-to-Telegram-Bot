"""Fast hermetic tests for RSSPlus native UI helpers (no Telegram token needed)."""
from __future__ import annotations

import ast
import html
import math
import re
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src/rssplus_ui.py"
code = SOURCE.read_text(encoding="utf-8")
tree = ast.parse(code)
functions = {"_page", "callback", "esc", "_terms"}
chosen = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in functions]
compiled = compile(ast.Module(body=chosen, type_ignores=[]), str(SOURCE), "exec")


class DashboardHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        namespace = {"ceil": math.ceil, "html": html, "re": re, "tr": lambda fa, en: fa}
        exec(compiled, namespace)
        cls.fn = namespace

    def test_callback_size_is_within_telegram_64_byte_limit(self):
        make = self.fn["callback"]
        self.assertEqual(make("rule", -1001234567890), b"rp:rule:-1001234567890")
        with self.assertRaises(ValueError):
            make("edit", "a" * 100)

    def test_pagination_clamps_to_valid_page(self):
        page = self.fn["_page"]
        self.assertEqual(page(2000, 7), (1, 2, 6))
        self.assertEqual(page(-2000, 0), (0, 1, 0))
        self.assertEqual(page(2, 17), (2, 3, 12))

    def test_escapes_untrusted_html(self):
        self.assertEqual(self.fn["esc"]("<b>&<script>"), "&lt;b&gt;&amp;&lt;script&gt;")

    def test_keyword_rules_are_bounded_and_deduped(self):
        terms = self.fn["_terms"]
        self.assertEqual(terms("science, علم\nscience"), ["science", "علم"])
        self.assertEqual(terms("پاک"), [])
        self.assertEqual(terms("CLEAR"), [])
        with self.assertRaises(ValueError):
            terms(",".join(str(x) for x in range(31)))
        with self.assertRaises(ValueError):
            terms("x" * 81)

    def test_privileged_registration_precedes_upstream_handlers(self):
        entry = (SOURCE.parent / "entrypoint.py").read_text(encoding="utf-8")
        self.assertLess(entry.index("rssplus_ui.install_handlers(bot)"),
                        entry.index("bot.add_event_handler(command.sub.cmd_sub,"))
        self.assertLess(entry.index("rssplus_ui.install_handlers(bot)"),
                        entry.index("bot.add_event_handler(command.misc.cmd_start,"))

    def test_routes_are_explicit(self):
        self.assertIn("if not _authorized(event):", code)
        self.assertIn('action in {"toggle", "send", "media", "confirm", "remove"}', code)
        self.assertIn("await inner.utils.activate_or_deactivate_sub", code)
        self.assertIn("await inner.sub.subs(target, [url])", code)
        self.assertIn("rssplus.save_config(config)", code)


if __name__ == "__main__":
    unittest.main()
