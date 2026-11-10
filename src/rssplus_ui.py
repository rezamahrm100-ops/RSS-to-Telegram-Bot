"""RSSPlus native Telegram management dashboard.

Data is queried on demand from the upstream ORM and RSSPlus policy engine.
The UI itself is only a view/controller, not a second subscription database.

Copyright (C) 2026 RSSPlus contributors
SPDX-License-Identifier: AGPL-3.0-or-later
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from dataclasses import dataclass
from math import ceil
from urllib.parse import urlsplit

from telethon import Button, events
from telethon.errors import MessageNotModifiedError

from . import db, env, rssplus
from .command import inner

LOG = logging.getLogger("RSStT.rssplus.ui")
PAGE_SIZE = 6
INPUT_TTL = 600
MAX_KEYWORDS = 30
_PENDING: dict[tuple[int, int], tuple[float, str, int, str]] = {}
_WRITE_LOCK = asyncio.Lock()


def _fa() -> bool:
    return os.environ.get("RSSPLUS_UI_LANG", "fa").lower().startswith("fa")


def tr(fa: str, en: str) -> str:
    return fa if _fa() else en


def esc(value: object, limit: int = 160) -> str:
    return html.escape(str(value if value is not None else "")[:limit], quote=True)


def callback(action: str, *args: object) -> bytes:
    data = ":".join(("rp", action, *(str(x) for x in args))).encode("ascii")
    if len(data) > 64:
        raise ValueError("Telegram callback payload is too large")
    return data


def button(label: str, action: str, *args: object):
    return Button.inline(label, callback(action, *args))


def _authorized(event) -> bool:
    return bool(getattr(event, "is_private", False)) and int(getattr(event, "sender_id", 0) or 0) in env.MANAGER


def _page(page: int, count: int) -> tuple[int, int, int]:
    total = max(1, ceil(count / PAGE_SIZE))
    page = min(max(0, page), total - 1)
    return page, total, page * PAGE_SIZE


def _pagination(action: str, page: int, total: int, *context: object) -> list:
    row = []
    if page > 0:
        row.append(button(tr("◀ قبلی", "◀ Previous"), action, page - 1, *context))
    row.append(button(f"{page + 1}/{total}", "noop"))
    if page < total - 1:
        row.append(button(tr("بعدی ▶", "Next ▶"), action, page + 1, *context))
    return row


@dataclass
class Screen:
    text: str
    rows: list


async def overview() -> Screen:
    feed_count, sub_count, active_count = await asyncio.gather(
        db.Feed.all().count(),
        db.Sub.all().count(),
        db.Sub.filter(state=1).count(),
    )
    target_ids = await db.Sub.all().values_list("user_id", flat=True)
    channel_count = len({int(cid) for cid in target_ids if int(cid) < 0})
    config = rssplus.load_config()
    queue_count = len(rssplus._pending_items(limit=1000)) if config else 0
    buttons = [
        [button(tr("📚 منابع", "📚 Feeds"), "feeds", 0),
         button(tr("📢 کانال‌ها", "📢 Channels"), "channels", 0)],
        [button(tr("🧩 قواعد انتشار", "🧩 Publishing rules"), "rules", 0),
         button(tr("🗂 صف بررسی", "🗂 Review queue"), "queue", 0)],
        [button(tr("⚙️ قابلیت‌ها", "⚙️ Capabilities"), "features"),
         button(tr("❔ راهنما", "❔ Help"), "help")],
        [button(tr("🔄 تازه‌سازی", "🔄 Refresh"), "home")],
    ]
    return Screen(
        tr("<b>RSSPlus | مرکز مدیریت</b>", "<b>RSSPlus | Control center</b>")
        + "\n\n"
        + tr("خوراک‌های ثبت‌شده: ", "Registered feeds: ") + f"<b>{feed_count}</b>\n"
        + tr("اشتراک‌ها: ", "Subscriptions: ") + f"<b>{sub_count}</b>\n"
        + tr("اشتراک‌های فعال: ", "Active subscriptions: ") + f"<b>{active_count}</b>\n"
        + tr("مقصدهای کانالی: ", "Channel destinations: ") + f"<b>{channel_count}</b>\n"
        + tr("منتظر تأیید: ", "Pending review: ") + f"<b>{queue_count}</b>\n\n"
        + tr("افزونهٔ انتشار: ", "Publishing extension: ")
        + ("✅" if config else tr("غیرفعال یا فاقد تنظیمات", "Disabled or unconfigured"))
        + "\n"
        + tr("اطلاعات هنگام بازکردن هر بخش از هسته خوانده می‌شوند.",
             "Data is refreshed from the core whenever a section is opened."),
        buttons,
    )


async def feeds(page: int, chat_id: int = 0) -> Screen:
    q = db.Sub.filter(user_id=chat_id) if chat_id else db.Sub.all()
    count = await q.count()
    page, total, offset = _page(page, count)
    subs = await q.order_by("-id").offset(offset).limit(PAGE_SIZE).prefetch_related("feed")
    title = tr("<b>📚 منابع و اشتراک‌ها</b>", "<b>📚 Feeds and subscriptions</b>")
    if chat_id:
        title += f"\n<code>{chat_id}</code>"
    rows = []
    for sub in subs:
        name = (sub.title or sub.feed.title or sub.feed.link).strip()[:31]
        marker = "🟢" if sub.state == 1 else "⏸"
        rows.append([button(f"{marker} {name}", "sub", sub.id, page, chat_id)])
    if not rows:
        title += "\n\n" + tr("هنوز اشتراکی ثبت نشده است.", "No subscriptions yet.")
    else:
        title += f"\n\n{count} " + tr("اشتراک؛ برای جزئیات لمس کنید.", "subscriptions; select to inspect.")
    if total > 1:
        rows.append(_pagination("feeds", page, total, chat_id))
    if chat_id:
        rows.append([button(tr("➕ افزودن خوراک", "➕ Add feed"), "add", chat_id)])
        rows.append([button(tr("↩ بازگشت به کانال", "↩ Channel"), "channel", chat_id)])
    else:
        rows.append([button(tr("↩ داشبورد", "↩ Dashboard"), "home")])
    return Screen(title, rows)


async def sub_detail(sub_id: int, page: int = 0, chat_id: int = 0) -> Screen:
    sub = await db.Sub.get_or_none(id=sub_id).prefetch_related("feed")
    if sub is None:
        return Screen(tr("اشتراک حذف شده یا دیگر وجود ندارد.", "Subscription no longer exists."),
                      [[button(tr("↩ منابع", "↩ Feeds"), "feeds", page, chat_id)]])
    feed = sub.feed
    state = tr("فعال", "Active") if sub.state == 1 else tr("متوقف", "Paused")
    send_modes = {0: tr("هوشمند", "Auto"), 1: "Telegraph", 2: tr("پیام", "Messages"), -1: tr("پیوند", "Link")}
    user = await db.User.get_or_none(id=sub.user_id)
    send_mode = sub.send_mode if sub.send_mode != -100 else (user.send_mode if user else 0)
    media_enabled = sub.display_media if sub.display_media != -100 else (user.display_media if user else 0)
    text = (tr("<b>جزئیات اشتراک</b>", "<b>Subscription details</b>")
            + f"\n\n<b>{esc(sub.title or feed.title, 250)}</b>"
            + f"\n{esc(feed.link, 1000)}"
            + "\n\n" + tr("مقصد: ", "Destination: ") + f"<code>{sub.user_id}</code>"
            + "\n" + tr("وضعیت: ", "Status: ") + state
            + "\n" + tr("وضعیت منبع: ", "Feed status: ") + ("✅" if feed.state == 1 else "⏸")
            + "\n" + tr("بازهٔ بررسی: ", "Check interval: ")
            + (f"{sub.interval} min" if sub.interval else tr("پیش‌فرض هسته", "Core default"))
            + "\n" + tr("روش ارسال: ", "Send mode: ") + send_modes.get(send_mode, str(send_mode))
            + "\n" + tr("رسانه: ", "Media: ") + ("✅" if media_enabled == 0 else "❌"))
    rows = [
        [button(tr("▶ فعال‌سازی", "▶ Resume") if sub.state != 1 else tr("⏸ توقف", "⏸ Pause"),
                "toggle", sub.id, page, chat_id),
         button(tr("🖼 تغییر رسانه", "🖼 Media"), "media", sub.id, page, chat_id)],
        [button(tr("📝 حالت انتشار", "📝 Send mode"), "send", sub.id, page, chat_id),
         button(tr("🗑 حذف اشتراک", "🗑 Remove"), "confirm", sub.id, page, chat_id)],
        [button(tr("↩ منابع", "↩ Feeds"), "feeds", page, chat_id)],
    ]
    return Screen(text, rows)


async def channels(page: int) -> Screen:
    ids = sorted({int(v) for v in await db.Sub.all().values_list("user_id", flat=True)
                  if int(v) < 0})
    page, total, offset = _page(page, len(ids))
    rows = []
    for cid in ids[offset:offset + PAGE_SIZE]:
        user = await db.User.get_or_none(id=cid)
        count = await db.Sub.filter(user_id=cid).count()
        name = getattr(user, "id", cid)
        rows.append([button(f"📢 {name} · {count}", "channel", cid)])
    if total > 1:
        rows.append(_pagination("channels", page, total))
    rows.append([button(tr("↩ داشبورد", "↩ Dashboard"), "home")])
    return Screen(
        tr("<b>📢 مقصدهای کانالی</b>\n", "<b>📢 Channel destinations</b>\n")
        + (tr("این فهرست از اشتراک‌های واقعی هسته تولید می‌شود؛ "
              "برای افزودن اولین اشتراک کانال، از فرمان اصلی /sub استفاده کنید.",
              "Generated from core subscriptions. Use upstream /sub for a new destination.")
           if not ids else f"{len(ids)} " + tr("مقصد فعال در پایگاه‌داده", "destinations registered")),
        rows,
    )


async def channel_detail(chat_id: int) -> Screen:
    count = await db.Sub.filter(user_id=chat_id).count()
    active = await db.Sub.filter(user_id=chat_id, state=1).count()
    policy = rssplus.policy_for(rssplus.load_config() or {}, chat_id)
    return Screen(
        tr("<b>📢 مدیریت مقصد</b>", "<b>📢 Destination</b>") + f"\n\n<code>{chat_id}</code>"
        + "\n" + tr("اشتراک‌ها: ", "Subscriptions: ") + str(count)
        + "\n" + tr("فعال: ", "Active: ") + str(active)
        + "\n" + tr("حالت: ", "Mode: ") + str(policy.get("mode", "auto")),
        [[button(tr("📚 منابع مقصد", "📚 Feed subscriptions"), "feeds", 0, chat_id)],
         [button(tr("➕ افزودن خوراک", "➕ Add feed"), "add", chat_id),
          button(tr("🧩 قواعد مقصد", "🧩 Rules"), "rule", chat_id)],
         [button(tr("↩ کانال‌ها", "↩ Channels"), "channels", 0)]],
    )


async def rules(page: int) -> Screen:
    config = rssplus.load_config() or {}
    configured = {int(k) for k in config.get("targets", {}) if str(k).lstrip("-").isdigit()}
    observed = {int(cid) for cid in await db.Sub.all().values_list("user_id", flat=True)
                if int(cid) < 0}
    ids = sorted(observed | configured)
    page, total, off = _page(page, len(ids))
    rows = []
    for cid in ids[off:off + PAGE_SIZE]:
        policy = rssplus.policy_for(config, cid)
        marker = "🔎" if policy.get("mode", "auto") == "review" else "🚀"
        rows.append([button(f"{marker} {cid}", "rule", cid)])
    if total > 1:
        rows.append(_pagination("rules", page, total))
    rows.append([button(tr("↩ داشبورد", "↩ Dashboard"), "home")])
    return Screen(
        tr("<b>🧩 قواعد انتشار</b>", "<b>🧩 Publishing rules</b>")
        + "\n\n" + (tr("تنظیمات RSSPlus غیرفعال است.", "RSSPlus is disabled.")
                   if not rssplus.load_config()
                   else tr("برای تنظیم قواعد هر مقصد وارد آن شوید.", "Select a destination to edit live rules.")),
        rows,
    )


async def rule_detail(chat_id: int) -> Screen:
    config = rssplus.load_config()
    if not config:
        return Screen(tr("ابتدا RSSPlus را در config/rssplus.json فعال کنید.",
                         "Enable RSSPlus in config/rssplus.json first."),
                      [[button(tr("↩ بازگشت", "↩ Back"), "rules", 0)]])
    policy = rssplus.policy_for(config, chat_id)
    mode = policy.get("mode", "auto")
    include = ", ".join(policy.get("include", [])) or "—"
    exclude = ", ".join(policy.get("exclude", [])) or "—"
    dedup = bool(policy.get("deduplicate", True))
    review_possible = bool(config.get("review_chat_id"))
    return Screen(
        tr("<b>🧩 قواعد این مقصد</b>", "<b>🧩 Destination rules</b>")
        + f"\n\n<code>{chat_id}</code>"
        + "\n" + tr("انتشار: ", "Publish mode: ") + ("🔎 " + tr("تأیید دستی", "Review") if mode == "review"
                                                   else "🚀 " + tr("خودکار", "Auto"))
        + "\n" + tr("جلوگیری از تکرار: ", "Deduplication: ") + ("✅" if dedup else "❌")
        + "\n" + tr("شامل: ", "Include: ") + esc(include, 500)
        + "\n" + tr("حذف: ", "Exclude: ") + esc(exclude, 500)
        + ("\n\n" + tr("⚠️ شناسهٔ گفت‌وگوی بررسی تنظیم نشده است.",
                       "⚠️ Review chat is not configured.") if not review_possible else ""),
        [[button(tr("🔁 تغییر حالت انتشار", "🔁 Change publish mode"), "mode", chat_id),
          button(tr("🔁 تغییر تکراری‌یاب", "🔁 Toggle dedupe"), "dedup", chat_id)],
         [button(tr("✏️ واژه‌های شامل", "✏️ Include words"), "edit", chat_id, "include"),
          button(tr("✏️ واژه‌های حذف", "✏️ Exclude words"), "edit", chat_id, "exclude")],
         [button(tr("↩ مدیریت مقصد", "↩ Destination"), "channel", chat_id)]],
    )


async def queue(page: int) -> Screen:
    if not rssplus.load_config():
        return Screen(tr("صف بررسی غیرفعال است.", "Review queue is disabled."),
                      [[button(tr("↩ داشبورد", "↩ Dashboard"), "home")]])
    items = rssplus._pending_items(limit=500)
    page, total, offset = _page(page, len(items))
    rows = []
    for item in items[offset:offset + PAGE_SIZE]:
        payload = __import__("json").loads(item["payload"])
        title = str(payload.get("entry", {}).get("title") or item["id"])[:28]
        rows.append([button("📰 " + title, "queued", item["id"])])
    if total > 1:
        rows.append(_pagination("queue", page, total))
    rows.append([button(tr("↩ داشبورد", "↩ Dashboard"), "home")])
    return Screen(
        tr("<b>🗂 صف تأیید خبر</b>", "<b>🗂 Moderation queue</b>")
        + f"\n\n{len(items)} " + tr("مورد؛ روی مورد بزنید تا دکمه‌های انتشار/رد دریافت کنید.",
                                    "items; select one to receive Approve/Reject buttons."),
        rows,
    )


async def features() -> Screen:
    config = rssplus.load_config()
    # Intentionally read availability from the actual upstream and addon at render time.
    from . import parsing
    capabilities = [
        (tr("خوراک RSS/Atom و مدیریت اشتراک", "RSS/Atom subscriptions"), callable(inner.sub.subs)),
        (tr("تنظیم قالب و رسانه", "Formatting and media"), callable(inner.customization.set_exhaustive_option)),
        (tr("مهاجرت OPML", "OPML import/export"), callable(inner.sub.export_opml)),
        (tr("ارسال چندرسانه‌ای", "Media publishing"), hasattr(parsing, "__path__")),
        (tr("فیلتر و حذف تکراری", "Filtering and deduplication"), bool(config)),
        (tr("صف بررسی", "Review queue"), bool(config and config.get("review_chat_id"))),
        (tr("اتصال وب‌هوک", "Webhook integration"), bool(config and config.get("webhook_url"))),
    ]
    lines = [f"{'✅' if available else '—'} {esc(name)}" for name, available in capabilities]
    return Screen(
        tr("<b>⚙️ قابلیت‌های واقعی سامانه</b>", "<b>⚙️ Live system capabilities</b>")
        + "\n\n" + "\n".join(lines)
        + "\n\n" + tr("این صفحه وجود قابلیت و تنظیمات فعلی را نمایش می‌دهد؛ "
                      "فعال بودن قابلیت به معنی تضمین موفقیت ارسال رسانه از هر منبع نیست.",
                      "Reflects implemented capabilities/configuration, not delivery guarantees."),
        [[button(tr("↩ داشبورد", "↩ Dashboard"), "home")]],
    )


def help_screen() -> Screen:
    return Screen(
        tr("<b>❔ راهنمای مدیریت RSSPlus</b>\n\n"
           "۱. از بخش کانال‌ها مقصد را انتخاب کنید.\n"
           "۲. در منابع، وضعیت هر اشتراک و قالب ارسال را تغییر دهید.\n"
           "۳. با «افزودن خوراک»، نشانی RSS را در پاسخ به پیام ربات بفرستید.\n"
           "۴. در قواعد، فیلترهای شامل/حذف، تکراری‌یاب و تأیید دستی را تنظیم کنید.\n"
           "۵. در صف بررسی، خبرها را تأیید یا رد کنید.\n\n"
           "فرمان‌های اصلی /sub، /set، /import و /export همچنان پشتیبانی می‌شوند.\n"
           "پنل تنها در گفت‌وگوی خصوصی و برای مدیر قابل استفاده است.",
           "<b>❔ RSSPlus help</b>\n\n"
           "1. Choose a destination under Channels.\n"
           "2. Review subscriptions and send options under Feeds.\n"
           "3. Add a feed by replying with its RSS URL.\n"
           "4. Configure filters, deduplication and manual review under Rules.\n"
           "5. Approve or reject posts in Queue.\n\n"
           "The upstream /sub, /set, /import and /export commands remain supported.\n"
           "The dashboard is private and restricted to the manager."),
        [[button(tr("↩ داشبورد", "↩ Dashboard"), "home")]],
    )


async def screen(action: str, args: list[str]) -> Screen:
    if action == "home":
        return await overview()
    if action == "feeds":
        return await feeds(int(args[0]), int(args[1]) if len(args) > 1 else 0)
    if action == "sub":
        return await sub_detail(int(args[0]), int(args[1]), int(args[2]))
    if action == "channels":
        return await channels(int(args[0]))
    if action == "channel":
        return await channel_detail(int(args[0]))
    if action == "rules":
        return await rules(int(args[0]))
    if action == "rule":
        return await rule_detail(int(args[0]))
    if action == "queue":
        return await queue(int(args[0]))
    if action == "features":
        return await features()
    if action == "help":
        return help_screen()
    return await overview()


async def _show(event, view: Screen, edit: bool = True) -> None:
    # edit current screen rather than flooding chat; fallback if message expired.
    try:
        if edit:
            await event.edit(view.text, buttons=view.rows, parse_mode="html", link_preview=False)
        else:
            await event.respond(view.text, buttons=view.rows, parse_mode="html", link_preview=False)
    except MessageNotModifiedError:
        pass
    except Exception:
        if not edit:
            raise
        await event.respond(view.text, buttons=view.rows, parse_mode="html", link_preview=False)


async def entry(event) -> None:
    raw = (getattr(event, "raw_text", "") or "").split()[0]
    is_start = raw.split("@")[0] == "/start"
    if not _authorized(event):
        if is_start:
            return  # Preserve the original public /start and language selection.
        if getattr(event, "is_private", False):
            await event.respond(tr("این پنل تنها برای مدیر فعال است.", "Manager access only."))
            raise events.StopPropagation
        return
    try:
        await _show(event, await overview(), edit=False)
    except Exception:
        LOG.exception("Unable to show RSSPlus dashboard")
        await event.respond(tr("خطا در بارگذاری داشبورد؛ لاگ سرور را بررسی کنید.",
                               "Unable to load dashboard; check server logs."))
    raise events.StopPropagation


async def _prompt(event, kind: str, target: int, field: str = "") -> None:
    if kind == "add":
        prompt = tr("نشانی کامل RSS/Atom را در پاسخ به همین پیام ارسال کنید.\nمهلت: ۱۰ دقیقه",
                    "Reply to this message with a complete RSS/Atom URL within 10 minutes.")
    else:
        prompt = tr(f"واژه‌های {field} را با ویرگول یا خط جدید بنویسید.\n"
                    "برای پاک‌کردن فهرست، «پاک» را ارسال کنید.\nمهلت: ۱۰ دقیقه",
                    f"Reply with {field} terms separated by commas or new lines.\n"
                    "Send CLEAR to empty the list. Expires in 10 minutes.")
    message = await event.respond(prompt, buttons=Button.force_reply(single_use=True, selective=True))
    _PENDING[(int(event.sender_id), int(message.id))] = (time.monotonic() + INPUT_TTL, kind, target, field)


def _terms(raw: str) -> list[str]:
    if raw.strip().casefold() in {"پاک", "clear", "-"}:
        return []
    pieces = [part.strip() for part in re.split(r"[,،\n]+", raw) if part.strip()]
    if not pieces or len(pieces) > MAX_KEYWORDS or any(len(part) > 80 for part in pieces):
        raise ValueError(tr("بین ۱ تا ۳۰ عبارت (هرکدام حداکثر ۸۰ حرف) بفرستید.",
                            "Send 1–30 phrases, each no longer than 80 characters."))
    return list(dict.fromkeys(pieces))


async def _set_policy(chat_id: int, key: str, value) -> None:
    async with _WRITE_LOCK:
        config = rssplus.load_config()
        if not config:
            raise ValueError(tr("RSSPlus غیرفعال است.", "RSSPlus is disabled."))
        if key == "mode" and value == "review" and not config.get("review_chat_id"):
            raise ValueError(tr("ابتدا review_chat_id را در تنظیمات تعیین کنید.",
                                "Configure review_chat_id first."))
        targets = config.setdefault("targets", {})
        policy = targets.setdefault(str(chat_id), {})
        policy[key] = value
        rssplus.save_config(config)


async def handle_callback(event) -> None:
    if not _authorized(event):
        await event.answer(tr("دسترسی مجاز نیست.", "Access denied."), alert=True)
        raise events.StopPropagation
    try:
        data = (event.data or b"").decode("ascii")
        parts = data.split(":")
        if len(parts) < 2 or parts[0] != "rp":
            return
        action, args = parts[1], parts[2:]
        if action == "noop":
            await event.answer()
            raise events.StopPropagation
        if action == "add":
            await event.answer()
            await _prompt(event, "add", int(args[0]))
        elif action == "edit":
            await event.answer()
            if args[1] not in {"include", "exclude"}:
                raise ValueError("Invalid rule")
            await _prompt(event, "edit", int(args[0]), args[1])
        elif action in {"toggle", "send", "media", "confirm", "remove"}:
            sub_id, page, cid = map(int, args[:3])
            sub = await db.Sub.get_or_none(id=sub_id)
            if sub is None or (cid and sub.user_id != cid):
                await event.answer(tr("این اشتراک دیگر وجود ندارد.", "Subscription not found."), alert=True)
                raise events.StopPropagation
            if action == "confirm":
                await _show(event, Screen(
                    tr("این اشتراک از هسته حذف شود؟", "Remove this subscription from the core?"),
                    [[button(tr("🗑 بله، حذف کن", "🗑 Yes, remove"), "remove", sub_id, page, cid)],
                     [button(tr("↩ انصراف", "↩ Cancel"), "sub", sub_id, page, cid)]]))
            else:
                if action == "toggle":
                    await inner.utils.activate_or_deactivate_sub(sub.user_id, sub, activate=sub.state != 1)
                elif action == "send":
                    await inner.customization.set_exhaustive_option(sub, "send_mode")
                elif action == "media":
                    await inner.customization.set_exhaustive_option(sub, "display_media")
                elif action == "remove":
                    await inner.sub.unsubs(sub.user_id, sub_ids=(sub.id,))
                await _show(event, await (feeds(page, cid) if action == "remove" else sub_detail(sub_id, page, cid)))
                await event.answer()
        elif action in {"mode", "dedup"}:
            cid = int(args[0])
            config = rssplus.load_config() or {}
            policy = rssplus.policy_for(config, cid)
            if action == "mode":
                await _set_policy(cid, "mode", "review" if policy.get("mode", "auto") == "auto" else "auto")
            else:
                await _set_policy(cid, "deduplicate", not bool(policy.get("deduplicate", True)))
            await _show(event, await rule_detail(cid))
            await event.answer()
        elif action == "queued":
            item = rssplus._get_item(args[0])
            if not item or item["status"] != "pending":
                await event.answer(tr("این خبر دیگر منتظر تأیید نیست.", "No longer pending."), alert=True)
            else:
                await rssplus._send_review(item, int(event.sender_id))
                await event.answer(tr("پیام بررسی ارسال شد.", "Review card sent."))
        else:
            await _show(event, await screen(action, args))
            await event.answer()
    except events.StopPropagation:
        raise
    except (ValueError, IndexError, KeyError) as exc:
        await event.answer(str(exc)[:160], alert=True)
    except Exception:
        LOG.exception("RSSPlus UI callback failed")
        await event.answer(tr("خطای داخلی؛ لاگ سرور را بررسی کنید.",
                              "Internal error; check server logs."), alert=True)
    raise events.StopPropagation


async def reply(event) -> None:
    if not _authorized(event) or not getattr(event, "is_reply", False):
        return
    key = (int(event.sender_id), int(event.reply_to_msg_id or 0))
    state = _PENDING.pop(key, None)
    if state is None:
        return
    expires, kind, target, field = state
    if time.monotonic() > expires:
        await event.respond(tr("زمان این درخواست تمام شده است؛ دوباره از پنل شروع کنید.",
                               "This request expired; start from the panel again."))
        raise events.StopPropagation
    try:
        if kind == "edit":
            await _set_policy(target, field, _terms(event.raw_text or ""))
            response = tr("✅ قواعد ذخیره شدند.", "✅ Rules saved.")
        else:
            url = (event.raw_text or "").strip()
            p = urlsplit(url)
            if (len(url) > 4096 or p.scheme not in {"http", "https"} or not p.hostname
                    or re.search(r"\s", url) or p.username or p.password):
                raise ValueError(tr("نشانی معتبر http/https وارد کنید.",
                                    "Provide a valid public HTTP(S) feed URL."))
            result = await inner.sub.subs(target, [url])
            if not result or result.get("success_count", 0) < 1:
                raise ValueError(tr("افزودن منبع ناموفق بود. آدرس RSS و دسترسی ربات را بررسی کنید.",
                                    "Failed to add feed; verify RSS URL and bot access."))
            response = tr("✅ منبع به هسته افزوده شد.", "✅ Feed added to the core.")
        await event.respond(response, buttons=[
            [button(tr("📢 بازگشت به مقصد", "📢 Return to destination"), "channel", target)],
            [button(tr("🏠 داشبورد", "🏠 Dashboard"), "home")],
        ])
    except ValueError as exc:
        await event.respond("⚠️ " + esc(exc, 200), parse_mode="html",
                            buttons=[[button(tr("↩ پنل", "↩ Panel"), "home")]])
    except Exception:
        LOG.exception("RSSPlus guided input failed")
        await event.respond(tr("خطای پردازش درخواست؛ لاگ را ببینید.",
                               "Request processing error; inspect logs."))
    raise events.StopPropagation


def install_handlers(bot) -> None:
    # Register before upstream's /start and automatic URL subscriptions.
    bot.add_event_handler(entry, events.NewMessage(
        pattern=r"^/(?:start|panel|dashboard|rssplus)(?:@\w+)?(?:\s.*)?$"))
    bot.add_event_handler(handle_callback, events.CallbackQuery(pattern=rb"^rp:[a-z0-9:.-]{1,60}$"))
    bot.add_event_handler(reply, events.NewMessage(incoming=True))
