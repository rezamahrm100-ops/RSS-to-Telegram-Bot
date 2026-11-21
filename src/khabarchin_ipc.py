"""Opt-in, Unix-socket control adapter INSIDE the running RSStT event loop.

This adapter calls upstream subscription functions rather than writing SQLite
from a second process. No TCP listener, shell, arbitrary SQL or public endpoint.
The Unix socket is available only when explicitly enabled on the container.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from . import db, env, rssplus
from .command.inner import sub as native_sub
from .command.inner import utils as native_utils
from .command.inner import customization as native_customization

_SOCKET_NAME = ".khabarchin-ipc.sock"
_MAX_BYTES = 2048
_runner = None
_site = None
_socket_path = None
_channels = frozenset()
_SETTING_ACTIONS = frozenset({
    "set_interval", "set_length_limit", "set_title", "set_hashtags", "set_option",
})
_DEFAULT_ACTIONS = frozenset({
    "set_default_interval", "set_default_length_limit", "set_default_option",
})
_BULK_ACTIONS = frozenset({"activate_all", "deactivate_all"})
_REVIEW_ACTIONS = frozenset({"review_approve", "review_reject"})
_SUB_OPTION_FIELDS = (
    "length_limit", "notify", "send_mode", "link_preview", "display_author",
    "display_media", "display_title", "display_entry_tags", "display_via", "style",
)



def _safe_string(value, limit: int):
    return (value is None or
            (isinstance(value, str) and 1 <= len(value) <= limit
             and all(ord(ch) >= 32 and ord(ch) != 127 for ch in value)))


def _valid_setting(value):
    action = value.get("action")
    if not _positive_int(value.get("subscription_id")):
        return False
    if action == "set_option":
        field = value.get("field")
        choices = native_customization.SUB_OPTIONS_EXHAUSTIVE_VALUES
        return (set(value) == {"action", "channel_id", "subscription_id", "field", "value"}
                and isinstance(field, str) and field in choices
                and type(value.get("value")) is int
                and value["value"] in (-100, *choices[field]))
    if set(value) != {"action", "channel_id", "subscription_id", "value"}:
        return False
    item = value.get("value")
    if action == "set_interval":
        return item is None or (type(item) is int and 1 <= item <= 1440)
    if action == "set_length_limit":
        return type(item) is int and (item == -100 or 0 <= item <= 4096)
    if action == "set_title":
        return _safe_string(item, 1024)
    if action == "set_hashtags":
        return _safe_string(item, 255)
    return False



def _valid_default_setting(payload):
    action = payload.get("action")
    if action == "set_default_option":
        field = payload.get("field")
        choices = native_customization.SUB_OPTIONS_EXHAUSTIVE_VALUES
        return (set(payload) == {"action", "channel_id", "field", "value"}
                and isinstance(field, str) and field in choices
                and type(payload.get("value")) is int
                and payload["value"] in choices[field])
    if set(payload) != {"action", "channel_id", "value"}:
        return False
    value = payload.get("value")
    if action == "set_default_interval":
        return value is None or (type(value) is int and 1 <= value <= 1440)
    if action == "set_default_length_limit":
        return type(value) is int and 0 <= value <= 4096
    return False


def _valid_bulk(payload):
    action = payload.get("action")
    return (set(payload) == {"action", "channel_id", "expected_count", "confirm"}
            and action in _BULK_ACTIONS
            and type(payload.get("expected_count")) is int
            and 0 <= payload["expected_count"] <= 10000
            and payload.get("confirm") == ("ACTIVATE_ALL" if action == "activate_all"
                                            else "DEACTIVATE_ALL"))


def _reset_response(sub):
    return {"success": True, "action": "reset_subscription",
            "channel_id": sub.user_id, "subscription_id": sub.id,
            "reset_fields": ["interval", *_SUB_OPTION_FIELDS]}


def _positive_int(value):
    return type(value) is int and 0 < value < 2**63


def _valid_channel(value):
    return (type(value) is int and -(2**63) < value < 0
            and value in _channels)


def _valid_url(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 1024:
        return False
    if any(ord(ch) < 32 for ch in value):
        return False
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        if parsed.username or parsed.password or parsed.fragment:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            pass
        return parsed.port in (None, 80, 443)
    except ValueError:
        return False


async def _dispatch(request):
    if request.content_type != "application/json" or request.content_length is None:
        raise web.HTTPBadRequest(text='{"error":"invalid framing"}', content_type="application/json")
    if not 1 <= request.content_length <= _MAX_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=_MAX_BYTES, actual_size=request.content_length)
    try:
        value = json.loads(await request.read())
    except (ValueError, UnicodeError):
        raise web.HTTPBadRequest(text='{"error":"invalid json"}', content_type="application/json")
    if not isinstance(value, dict) or not _valid_channel(value.get("channel_id")):
        raise web.HTTPForbidden(text='{"error":"channel not approved"}', content_type="application/json")
    action = value.get("action")
    if action == "subscribe":
        if set(value) != {"action", "channel_id", "url"} or not _valid_url(value.get("url")):
            raise web.HTTPBadRequest(text='{"error":"invalid subscription request"}', content_type="application/json")
    elif action in ("unsubscribe", "activate", "deactivate"):
        if set(value) != {"action", "channel_id", "subscription_id"} or not _positive_int(value.get("subscription_id")):
            raise web.HTTPBadRequest(text='{"error":"invalid subscription request"}', content_type="application/json")
    elif action in _SETTING_ACTIONS:
        if not _valid_setting(value):
            raise web.HTTPBadRequest(text='{"error":"invalid settings request"}', content_type="application/json")
    elif action in _DEFAULT_ACTIONS:
        if not _valid_default_setting(value):
            raise web.HTTPBadRequest(text='{"error":"invalid default settings"}', content_type="application/json")
    elif action in _BULK_ACTIONS:
        if not _valid_bulk(value):
            raise web.HTTPBadRequest(text='{"error":"invalid bulk confirmation"}', content_type="application/json")
    elif action == "reset_subscription":
        if set(value) != {"action", "channel_id", "subscription_id"} or not _positive_int(value.get("subscription_id")):
            raise web.HTTPBadRequest(text='{"error":"invalid reset request"}', content_type="application/json")
    elif action in _REVIEW_ACTIONS:
        if (set(value) != {"action", "channel_id", "review_id", "confirm"}
                or not isinstance(value.get("review_id"), str)
                or not re.fullmatch(r"[0-9a-f]{16}", value["review_id"])
                or value.get("confirm") != ("PUBLISH_REVIEW" if action == "review_approve"
                                             else "REJECT_REVIEW")):
            raise web.HTTPBadRequest(text='{"error":"invalid review confirmation"}', content_type="application/json")
    else:
        raise web.HTTPBadRequest(text='{"error":"unsupported operation"}', content_type="application/json")

    channel_id = value["channel_id"]
    try:
        if action in _REVIEW_ACTIONS:
            try:
                status = await rssplus.act_on_review(
                    value["review_id"], channel_id,
                    "approve" if action == "review_approve" else "reject")
            except ValueError:
                return web.json_response({"error": "review unavailable or already handled"}, status=409)
            return web.json_response({"success": True, "action": action,
                                      "channel_id": channel_id,
                                      "review_id": value["review_id"], "status": status})
        if action == "subscribe":
            # Existing RSStT logic performs RSS parsing, ORM writes, limits and scheduler updates.
            outcome = await native_sub.subs(channel_id, (value["url"],), lang="en")
            first = (outcome or {}).get("sub_d_l", ())
            item = first[0].get("sub") if first else None
            if item is None:
                return web.json_response({"error": "subscription unchanged or rejected"}, status=409)
            return web.json_response({"success": True, "action": action,
                                      "channel_id": channel_id, "subscription_id": item.id})
        if action in _DEFAULT_ACTIONS:
            user = await db.User.get_or_none(id=channel_id)
            if user is None:
                return web.json_response({"error": "destination not registered"}, status=404)
            if action == "set_default_interval":
                await native_customization.set_interval(
                    user, -100 if value["value"] is None else value["value"])
                field = "interval"
            elif action == "set_default_length_limit":
                await native_customization.set_length_limit(user, value["value"])
                field = "length_limit"
            else:
                field = value["field"]
                if getattr(user, field) != value["value"]:
                    setattr(user, field, value["value"])
                    await user.save(update_fields=(field,))
            return web.json_response({"success": True, "action": action,
                                      "channel_id": channel_id, "field": field,
                                      "value": getattr(user, field)})
        if action in _BULK_ACTIONS:
            count = await db.Sub.filter(user_id=channel_id).count()
            if count != value["expected_count"]:
                return web.json_response({"error": "subscription count changed"}, status=409)
            changed = await native_utils.activate_or_deactivate_all_subs(
                channel_id, activate=(action == "activate_all"))
            return web.json_response({"success": True, "action": action,
                                      "channel_id": channel_id, "total_count": count,
                                      "affected_count": len(changed)})
        if action == "reset_subscription":
            sub = await db.Sub.get_or_none(id=value["subscription_id"], user_id=channel_id)
            if sub is None:
                return web.json_response({"error": "subscription not found"}, status=404)
            interval_was_custom = sub.interval is not None
            sub.interval = None
            for field in _SUB_OPTION_FIELDS:
                setattr(sub, field, -100)
            await sub.save(update_fields=("interval", *_SUB_OPTION_FIELDS))
            if interval_was_custom:
                await native_utils.update_interval(sub)
            return web.json_response(_reset_response(sub))
        if action in _SETTING_ACTIONS:
            # Look up through the live bot ORM; never let API mutate another channel's subscription.
            sub = await db.Sub.get_or_none(id=value["subscription_id"], user_id=channel_id)
            if sub is None:
                return web.json_response({"error": "subscription not found"}, status=404)
            if action == "set_interval":
                await native_customization.set_interval(
                    sub, -100 if value["value"] is None else value["value"])
                field = "interval"
            elif action == "set_length_limit":
                await native_customization.set_length_limit(sub, value["value"])
                field = "length_limit"
            elif action == "set_title":
                await native_customization.set_sub_title(sub, value["value"])
                field = "title"
            elif action == "set_hashtags":
                await native_customization.set_sub_hashtags(sub, value["value"])
                field = "tags"
            else:
                field = value["field"]
                # Upstream's button cycles the same enum values; API selects
                # one value deterministically using the same native ORM model.
                if getattr(sub, field) != value["value"]:
                    setattr(sub, field, value["value"])
                    await sub.save(update_fields=(field,))
            return web.json_response({"success": True, "action": action,
                                      "channel_id": channel_id,
                                      "subscription_id": sub.id,
                                      "field": field, "value": getattr(sub, field)})
        if action == "unsubscribe":
            outcome = await native_sub.unsub(channel_id, sub_id=value["subscription_id"], lang="en")
            if outcome.get("sub") is None:
                return web.json_response({"error": "subscription not found or rejected"}, status=404)
        else:
            item = await native_utils.activate_or_deactivate_sub(
                channel_id, value["subscription_id"], activate=(action == "activate"))
            if item is None:
                return web.json_response({"error": "subscription not found"}, status=404)
    except Exception:
        # Never return feed URLs, Telegram credentials, raw exceptions or DB details.
        return web.json_response({"error": "native RSStT operation failed"}, status=503)
    return web.json_response({"success": True, "action": action, "channel_id": channel_id,
                              "subscription_id": value["subscription_id"]})


async def start():
    global _runner, _site, _socket_path, _channels
    if os.environ.get("KHABARCHIN_IPC_ENABLED") != "1":
        return
    raw = os.environ.get("KHABARCHIN_MANAGED_CHANNEL_IDS", "")
    try:
        channel_ids = tuple(int(i.strip()) for i in raw.split(",") if i.strip())
    except ValueError as exc:
        raise RuntimeError("Invalid Khabarchin managed channel list") from exc
    if not channel_ids or any(type(i) is not int or i >= 0 for i in channel_ids):
        raise RuntimeError("Khabarchin IPC requires explicit negative channel IDs")
    _channels = frozenset(channel_ids)
    path = Path(env.config_folder_path) / _SOCKET_NAME
    if path.is_symlink() or path.exists():
        # Do not remove a live or unknown socket. Operator must verify stale state.
        raise RuntimeError("Khabarchin IPC socket path is occupied")
    _socket_path = path
    app = web.Application(client_max_size=_MAX_BYTES)
    app.router.add_post("/native/v1/command", _dispatch)
    _runner = web.AppRunner(app, access_log=None)
    try:
        await _runner.setup()
        _site = web.UnixSite(_runner, str(path))
        await _site.start()
        if not path.exists() or not stat.S_ISSOCK(path.stat().st_mode):
            raise RuntimeError("Khabarchin IPC socket unavailable")
        os.chmod(path, 0o600)
    except Exception:
        await _runner.cleanup()
        _runner = None
        if path.is_socket() and not path.is_symlink():
            path.unlink()
        raise


async def stop():
    global _runner, _site, _socket_path
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
        _site = None
    if _socket_path and _socket_path.is_socket() and not _socket_path.is_symlink():
        _socket_path.unlink()
    _socket_path = None
