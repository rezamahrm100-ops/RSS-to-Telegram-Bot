"""Opt-in, Unix-socket control adapter INSIDE the running RSStT event loop.

This adapter calls upstream subscription functions rather than writing SQLite
from a second process. No TCP listener, shell, arbitrary SQL or public endpoint.
The Unix socket is available only when explicitly enabled on the container.
"""
from __future__ import annotations

import ipaddress
import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from . import env
from .command.inner import sub as native_sub
from .command.inner import utils as native_utils

_SOCKET_NAME = ".khabarchin-ipc.sock"
_MAX_BYTES = 2048
_runner = None
_site = None
_socket_path = None
_channels = frozenset()


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
    else:
        raise web.HTTPBadRequest(text='{"error":"unsupported operation"}', content_type="application/json")

    channel_id = value["channel_id"]
    try:
        if action == "subscribe":
            # Existing RSStT logic performs RSS parsing, ORM writes, limits and scheduler updates.
            outcome = await native_sub.subs(channel_id, (value["url"],), lang="en")
            first = (outcome or {}).get("sub_d_l", ())
            item = first[0].get("sub") if first else None
            if item is None:
                return web.json_response({"error": "subscription unchanged or rejected"}, status=409)
            return web.json_response({"success": True, "action": action,
                                      "channel_id": channel_id, "subscription_id": item.id})
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
