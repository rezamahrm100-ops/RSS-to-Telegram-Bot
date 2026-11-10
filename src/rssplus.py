"""Optional moderation and publishing layer for RSS-to-Telegram-Bot.

Copyright (C) 2026 RSSPlus contributors
SPDX-License-Identifier: AGPL-3.0-or-later
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

logger = logging.getLogger("RSStT.rssplus")
_DB_LOCK = asyncio.Lock()
_TRACKING = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid"}


def config_path() -> Path:
    return Path(os.environ.get("RSSPLUS_CONFIG", "/app/config/rssplus.json"))


def load_config() -> dict | None:
    path = config_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("RSSPlus settings must be a JSON object")
        return data if data.get("enabled", False) else None
    except (OSError, ValueError) as exc:
        logger.error("Invalid RSSPlus configuration: %s", exc)
        # Fail closed rather than publishing unexpectedly after an invalid edit.
        raise


def normalized_link(link: str | None) -> str:
    if not link:
        return ""
    try:
        p = urlsplit(link.strip())
        if p.scheme.lower() not in {"http", "https"} or not p.hostname:
            return link.strip()
        query = urlencode(sorted(
            (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING
        ))
        return urlunsplit(("https", p.netloc.lower(), p.path.rstrip("/") or "/", query, ""))
    except ValueError:
        return link.strip()


def plain_text(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value or "")).split())


def fingerprint(post) -> str:
    link = normalized_link(getattr(post, "link", None))
    if link:
        key = "link:" + link
    else:
        title = (getattr(post, "title", "") or "").casefold().strip()
        body = plain_text(getattr(post, "html", "") or "")[:300].casefold()
        key = "text:" + title + "\n" + body
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def policy_for(config: dict, chat_id: int) -> dict:
    defaults = config.get("default", {})
    targets = config.get("targets", {})
    overrides = targets.get(str(chat_id), {}) if isinstance(targets, dict) else {}
    return {**(defaults or {}), **(overrides or {})}


def is_filtered(post, policy: dict) -> bool:
    haystack = " ".join(
        (getattr(post, "title", "") or "", plain_text(getattr(post, "html", "") or ""))
    ).casefold()
    include = [str(s).casefold() for s in policy.get("include", []) if str(s).strip()]
    exclude = [str(s).casefold() for s in policy.get("exclude", []) if str(s).strip()]
    return (bool(include) and not any(s in haystack for s in include)) or any(s in haystack for s in exclude)


def _db_path() -> Path:
    from . import env
    return Path(env.config_folder_path) / "rssplus.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        fp TEXT NOT NULL,
        sub_id INTEGER NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(chat_id, fp)
    )""")
    con.commit()
    return con


def _item_id(chat_id: int, fp: str) -> str:
    return hashlib.sha256(f"{chat_id}:{fp}".encode()).hexdigest()[:16]


def _archive_payload(feed, entry: dict) -> str:
    return json.dumps(
        {"feed_title": feed.title, "feed_link": feed.link, "entry": entry},
        default=str, ensure_ascii=False,
    )


def _record(chat_id: int, fp: str, sub_id: int, payload: str, status: str) -> tuple[str, bool]:
    key = _item_id(chat_id, fp)
    con = _connect()
    try:
        with con:
            cur = con.execute(
                "INSERT OR IGNORE INTO items (id, chat_id, fp, sub_id, payload, status) "
                "VALUES (?, ?, ?, ?, ?, ?)", (key, chat_id, fp, sub_id, payload, status),
            )
            return key, cur.rowcount == 1
    finally:
        con.close()


def _transition(key: str, old: str, new: str) -> bool:
    con = _connect()
    try:
        with con:
            cur = con.execute("UPDATE items SET status=? WHERE id=? AND status=?", (new, key, old))
            return cur.rowcount == 1
    finally:
        con.close()


def _get_item(key: str):
    con = _connect()
    try:
        row = con.execute("SELECT * FROM items WHERE id=?", (key,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _pending_items(limit: int = 20) -> list[dict]:
    con = _connect()
    try:
        return [dict(row) for row in con.execute(
            "SELECT * FROM items WHERE status='pending' ORDER BY created_at DESC LIMIT ?", (limit,)
        )]
    finally:
        con.close()


async def _webhook(config: dict, post, chat_id: int, status: str) -> None:
    url = str(config.get("webhook_url", "") or "").strip()
    if not url:
        return
    if not url.startswith("https://"):
        logger.error("RSSPlus webhook_url must use HTTPS; webhook skipped")
        return
    data = json.dumps({
        "event": "post_published", "status": status, "channel_id": chat_id,
        "title": getattr(post, "title", None), "link": getattr(post, "link", None),
        "feed": getattr(post, "feed_title", None)
    }, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"}
    token = str(config.get("webhook_token", "") or "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def send() -> None:
        with urlopen(Request(url, data=data, headers=headers, method="POST"), timeout=8) as response:
            response.read(1024)

    try:
        await asyncio.to_thread(send)
    except Exception as exc:
        logger.warning("RSSPlus webhook delivery failed (no automatic retry): %s", exc)


def _preview_text(item: dict) -> str:
    payload = json.loads(item["payload"])
    entry = payload["entry"]
    title = html.escape(str(entry.get("title") or "بدون عنوان")[:350])
    link = normalized_link(entry.get("link", ""))
    source = html.escape(str(payload.get("feed_title", ""))[:160])
    url_part = f'\n{html.escape(link[:800])}' if link else ""
    return (
        f"📰 <b>در انتظار تأیید</b>\n<b>{title}</b>\n"
        f"منبع: {source}\nمقصد: <code>{item['chat_id']}</code>{url_part}"
    )


async def _send_review(item: dict, review_chat_id: int) -> None:
    from telethon import Button
    from . import env
    key = item["id"]
    buttons = [[
        Button.inline("✅ انتشار", data=f"rssplus:approve:{key}".encode()),
        Button.inline("❌ رد", data=f"rssplus:reject:{key}".encode()),
    ]]
    await env.bot.send_message(
        review_chat_id, _preview_text(item), buttons=buttons,
        parse_mode="html", link_preview=False,
    )


async def process_post(*, sub, post, entry, feed, send_func) -> bool:
    """Return True when handled by RSSPlus, False to use unmodified upstream behavior."""
    config = load_config()
    if not config:
        return False
    chat_id = int(sub.user_id)
    policy = policy_for(config, chat_id)
    if is_filtered(post, policy):
        logger.info("RSSPlus blocked post for %s: %s", chat_id, getattr(post, "link", ""))
        return True
    mode = policy.get("mode", "auto")
    if mode not in {"auto", "review"}:
        raise ValueError(f"RSSPlus mode must be 'auto' or 'review', got {mode!r}")
    if mode == "review" and not config.get("review_chat_id"):
        raise ValueError("review_chat_id is required when review mode is enabled")
    if not policy.get("deduplicate", True) and mode == "auto":
        await send_func(sub, post)
        await _webhook(config, post, chat_id, "published")
        return True
    async with _DB_LOCK:
        key, inserted = _record(
            chat_id, fingerprint(post), int(sub.id), _archive_payload(feed, entry),
            "pending" if mode == "review" else "publishing",
        )
    if not inserted:
        return True
    if mode == "review":
        try:
            await _send_review(_get_item(key), int(config["review_chat_id"]))
        except Exception:
            logger.exception("Could not deliver review preview; item remains queued: %s", key)
            raise
    else:
        try:
            await send_func(sub, post)
        except Exception:
            _transition(key, "publishing", "pending")
            raise
        else:
            _transition(key, "publishing", "published")
            await _webhook(config, post, chat_id, "published")
    return True


async def handle_callback(event) -> None:
    from . import env, db
    from .parsing.post import get_post_from_entry
    if int(event.sender_id or 0) not in {int(x) for x in env.MANAGER}:
        await event.answer("اجازهٔ مدیریت ندارید", alert=True)
        return
    match = re.fullmatch(rb"rssplus:(approve|reject):([0-9a-f]{16})", event.data or b"")
    if not match:
        return
    action = match.group(1).decode()
    key = match.group(2).decode()
    item = _get_item(key)
    if not item or item["status"] != "pending":
        await event.answer("قبلاً بررسی شده یا در حال ارسال است", alert=True)
        return
    if action == "reject":
        if not _transition(key, "pending", "rejected"):
            await event.answer("قبلاً بررسی شده", alert=True)
            return
        await event.edit("❌ خبر رد شد.", buttons=None)
        await event.answer()
        return
    if not _transition(key, "pending", "publishing"):
        await event.answer("قبلاً بررسی شده", alert=True)
        return
    try:
        payload = json.loads(item["payload"])
        sub = await db.Sub.get_or_none(id=item["sub_id"])
        if sub is None or int(sub.user_id) != item["chat_id"]:
            raise ValueError("Subscription no longer exists or destination has changed")
        post = await get_post_from_entry(
            payload["entry"], payload["feed_title"], payload["feed_link"]
        )
        await post.send_formatted_post_according_to_sub(sub)
        _transition(key, "publishing", "published")
        await event.edit("✅ خبر منتشر شد.", buttons=None)
        config = load_config()
        if config:
            await _webhook(config, post, item["chat_id"], "published")
        await event.answer()
    except Exception as exc:
        _transition(key, "publishing", "pending")
        logger.exception("RSSPlus approval failed: %s", key)
        await event.answer(f"خطا در انتشار: {str(exc)[:120]}", alert=True)


async def handle_pending_command(event) -> None:
    from . import env
    if int(event.sender_id or 0) not in {int(x) for x in env.MANAGER}:
        return
    if not event.is_private:
        return
    rows = _pending_items(limit=20)
    if not rows:
        await event.reply("صف تأیید خالی است.")
        return
    await event.reply(f"{len(rows)} مورد آخر صف تأیید:")
    for item in rows:
        await _send_review(item, int(event.sender_id))
