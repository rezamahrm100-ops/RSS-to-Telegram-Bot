#!/usr/bin/env python3
"""Opt-in, loopback-only RSStT/RSSPlus management API. Standard library only.

Reads the existing databases without taking ownership of the bot process. Only
an explicitly enabled, compare-and-swap policy update can write rssplus.json.
Never offers a generic shell, SQL query, arbitrary filesystem or network API.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import socket
import hmac
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

VERSION = "0.3.0"
MAX_REQUEST_BYTES = 8192
MAX_CONFIG_BYTES = 262144
WRITE_LOCK = threading.Lock()
RULE_PATH = re.compile(r"/v1/rules/(-?\d{1,20})\Z")
SUB_SETTING_PATH = re.compile(r"/v1/subscriptions/([1-9][0-9]{0,18})/settings\Z")
ALLOWED_RULE_FIELDS = frozenset({"mode", "deduplicate", "include", "exclude"})
NATIVE_ACTIONS = frozenset({
    "subscribe", "unsubscribe", "activate", "deactivate",
    "set_title", "set_hashtags", "set_interval", "set_length_limit", "set_option",
})
SETTING_ACTIONS = frozenset({
    "set_title", "set_hashtags", "set_interval", "set_length_limit", "set_option",
})
SETTING_CHOICES = {
    "notify": (1, 0),
    "send_mode": (0, 1, 2, -1),
    "link_preview": (0, 1, -1),
    "display_media": (0, 1, -1),
    "display_author": (0, 1, -1),
    "display_via": (0, 1, -3, -1, -4, -2),
    "display_title": (0, 1, -1),
    "display_entry_tags": (1, -1),
    "style": (0, 1),
}


def _valid_setting_action(body: dict) -> bool:
    action = body["action"]
    if action == "set_option":
        field = body.get("field")
        return (set(body) == {"action", "channel_id", "subscription_id", "field", "value"}
                and isinstance(field, str) and field in SETTING_CHOICES
                and type(body.get("value")) is int
                and body["value"] in (-100, *SETTING_CHOICES[field]))
    if set(body) != {"action", "channel_id", "subscription_id", "value"}:
        return False
    item = body.get("value")
    if action == "set_interval":
        return item is None or (type(item) is int and 1 <= item <= 1440)
    if action == "set_length_limit":
        return type(item) is int and (item == -100 or 0 <= item <= 4096)
    if action in ("set_title", "set_hashtags"):
        limit = 1024 if action == "set_title" else 255
        return (item is None or (isinstance(item, str)
                and 1 <= len(item) <= limit
                and all(ord(ch) >= 32 and ord(ch) != 127 for ch in item)))
    return False



class ApiError(Exception):
    def __init__(self, code: int, message: str):
        self.code, self.message = code, message


def _safe_read(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ApiError(503, "required local file unavailable")
    return path.read_bytes()


def _sqlite_read(db: Path, query: str, args=()) -> list[dict]:
    if db.is_symlink() or not db.is_file():
        raise ApiError(503, "SQLite database unavailable")
    connection = None
    try:
        uri = "file:" + quote(str(db.resolve()), safe="/") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, args).fetchall()]
    except (sqlite3.Error, OSError):
        raise ApiError(503, "SQLite database unreadable") from None
    finally:
        if connection is not None:
            connection.close()


def _read_config(path: Path) -> tuple[dict, str]:
    data = _safe_read(path, MAX_CONFIG_BYTES)
    try:
        config = json.loads(data)
    except (ValueError, UnicodeError):
        raise ApiError(503, "RSSPlus configuration invalid") from None
    if not isinstance(config, dict):
        raise ApiError(503, "RSSPlus configuration invalid")
    return config, '"' + hashlib.sha256(data).hexdigest() + '"'


def _read_optional_config(path: Path) -> tuple[dict | None, str | None]:
    """Absence means RSSPlus has not been configured, not a broken RSStT database.

    Reject symlinks (including dangling ones) and malformed/oversized present files.
    Writes always use the strict _read_config path and never create RSSPlus config.
    """
    if path.is_symlink():
        raise ApiError(503, "RSSPlus configuration unavailable")
    if not path.exists():
        return None, None
    return _read_config(path)


def _public_policy(value) -> dict:
    """Never disclose unknown/future secret-bearing RSSPlus policy fields."""
    if not isinstance(value, dict):
        return {}
    return {k: value[k] for k in ("mode", "deduplicate", "include", "exclude") if k in value}


def _validate_rule(value) -> dict:
    if not isinstance(value, dict) or not value or set(value) - ALLOWED_RULE_FIELDS:
        raise ApiError(400, "invalid or empty rule object")
    rule = {}
    for key, item in value.items():
        if key == "mode":
            if not isinstance(item, str) or item not in ("auto", "review"):
                raise ApiError(400, "invalid mode")
        elif key == "deduplicate":
            if not isinstance(item, bool):
                raise ApiError(400, "deduplicate must be boolean")
        elif key in ("include", "exclude"):
            if (not isinstance(item, list) or len(item) > 30 or
                any(not isinstance(term, str) or not term.strip() or len(term) > 80
                    for term in item)):
                raise ApiError(400, "invalid filter terms")
        rule[key] = item
    return rule


def _atomic_json_write(path: Path, config: dict) -> None:
    new_bytes = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode()
    if len(new_bytes) > MAX_CONFIG_BYTES:
        raise ApiError(413, "configuration too large")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".khabarchin-", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            os.chmod(temp, 0o600)
            stream.write(new_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or not path.is_file():
            raise ApiError(409, "configuration changed")
        os.replace(temp, path)
        temp = None
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over the existing private Unix socket; never proxies over the network."""

    def __init__(self, path: Path):
        super().__init__("localhost", timeout=8)
        self.socket_path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class Controller(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, config_dir: Path, db: Path, token: bytes,
                 allow_rules_write: bool = False, allow_subscription_write: bool = False,
                 allowed_channels=()):
        self.config_dir = config_dir
        self.db = db
        self.token = token
        self.allow_rules_write = allow_rules_write
        self.allow_subscription_write = allow_subscription_write
        self.allowed_channels = frozenset(allowed_channels)
        self.ipc_socket = config_dir / ".khabarchin-ipc.sock"
        self.config_file = config_dir / "rssplus.json"
        self.review_db = config_dir / "rssplus.sqlite3"
        self.audit_log = config_dir / ".khabarchin-api-audit.jsonl"
        super().__init__(address, Handler)

    def audit(self, event: str, outcome: str, target: str = "") -> None:
        line = json.dumps({"utc": datetime.now(timezone.utc).isoformat(),
                           "event": event, "outcome": outcome, "target": target},
                          separators=(",", ":")) + "\n"
        if self.audit_log.is_symlink():
            raise ApiError(503, "audit file is not safe")
        fd = os.open(self.audit_log, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)


class Handler(BaseHTTPRequestHandler):
    server: Controller
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # Never log headers, bearer credentials, feed URLs or raw payloads.

    def _json(self, code: int, data: dict, etag: str | None = None):
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        if etag is not None:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(payload)

    def _preflight(self):
        host = self.headers.get("Host", "")
        # Host allowlisting prevents DNS-rebinding abuse even on loopback.
        expected = {"localhost", "127.0.0.1", f"localhost:{self.server.server_port}",
                    f"127.0.0.1:{self.server.server_port}"}
        if host not in expected:
            raise ApiError(403, "invalid host")
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer ") or not hmac.compare_digest(
                value[7:].encode(), self.server.token):
            raise ApiError(401, "unauthorized")

    def _strict_json(self) -> dict:
        length_value = self.headers.get("Content-Length", "")
        if (not length_value.isdecimal()
                or not 1 <= int(length_value) <= 2048
                or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                or self.headers.get("Transfer-Encoding")):
            raise ApiError(400, "invalid JSON request framing")
        try:
            value = json.loads(self.rfile.read(int(length_value)))
        except (ValueError, UnicodeError):
            raise ApiError(400, "invalid JSON") from None
        if not isinstance(value, dict):
            raise ApiError(400, "JSON object required")
        return value

    def _limit(self) -> int:
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=False)
        if set(query) - {"limit"}:
            raise ApiError(400, "invalid query parameter")
        try:
            count = int(query.get("limit", ["20"])[0])
        except ValueError:
            raise ApiError(400, "invalid limit") from None
        if not 1 <= count <= 50:
            raise ApiError(400, "limit must be between 1 and 50")
        return count

    def do_GET(self):
        try:
            self._preflight()
            path = urlsplit(self.path).path
            if path == "/v1/health":
                return self._json(200, {"gateway": "ok", "version": VERSION,
                                        "bot_running": "not_checked"})
            if path == "/v1/capabilities":
                return self._json(200, {"read": ["health", "status", "feeds", "subscriptions",
                                                 "subscriptions.settings", "rules", "reviews"],
                                        "write": ((["rules.update"] if self.server.allow_rules_write else [])
                                                   + (["subscriptions.subscribe", "subscriptions.unsubscribe",
                                                       "subscriptions.activate", "subscriptions.deactivate",
                                                        "subscriptions.set_title", "subscriptions.set_hashtags",
                                                        "subscriptions.set_interval", "subscriptions.set_length_limit",
                                                        "subscriptions.set_option"]
                                                      if self.server.allow_subscription_write else [])),
                                        "unsupported": ["shell", "sql", "arbitrary_file", "deploy", "publish"]})
            if path == "/v1/status":
                counts = _sqlite_read(self.server.db,
                    "SELECT (SELECT COUNT(*) FROM feed) AS feeds, "
                    "(SELECT COUNT(*) FROM sub) AS subscriptions, "
                    "(SELECT COUNT(*) FROM sub WHERE state=1) AS active_subscriptions")
                cfg, _ = _read_optional_config(self.server.config_file)
                return self._json(200, {"db": "readable", **counts[0],
                                        "rssplus_configured": cfg is not None,
                                        "rssplus_enabled": bool(cfg and cfg.get("enabled") is True),
                                        "bot_running": "not_checked"})
            if path == "/v1/feeds":
                return self._json(200, {"items": _sqlite_read(self.server.db,
                    "SELECT id,title,link,state,error_count FROM feed ORDER BY id DESC LIMIT ?",
                    (self._limit(),))})
            if path == "/v1/subscriptions":
                return self._json(200, {"items": _sqlite_read(self.server.db,
                    "SELECT s.id,s.user_id AS channel_id,s.feed_id,s.state,s.title,"
                    "f.title AS feed_title,f.link AS feed_link FROM sub AS s "
                    "JOIN feed AS f ON s.feed_id=f.id ORDER BY s.id DESC LIMIT ?",
                    (self._limit(),))})
            match = SUB_SETTING_PATH.fullmatch(path)
            if match:
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if set(query) != {"channel_id"} or len(query["channel_id"]) != 1:
                    raise ApiError(400, "channel_id required")
                try:
                    channel = int(query["channel_id"][0])
                except ValueError:
                    raise ApiError(400, "invalid channel_id") from None
                if (type(channel) is not int or channel not in self.server.allowed_channels
                        or str(channel) != query["channel_id"][0]):
                    raise ApiError(403, "channel not approved")
                rows = _sqlite_read(self.server.db,
                    "SELECT id,user_id AS channel_id,feed_id,state,title,tags,interval,"
                    "notify,send_mode,length_limit,link_preview,display_author,"
                    "display_via,display_title,display_entry_tags,style,display_media "
                    "FROM sub WHERE id=? AND user_id=? LIMIT 1", (int(match.group(1)), channel))
                if not rows:
                    raise ApiError(404, "subscription not found")
                return self._json(200, {"settings": rows[0]})
            if path == "/v1/reviews":
                limit = self._limit()
                if self.server.review_db.is_symlink():
                    raise ApiError(503, "RSSPlus review database unavailable")
                if not self.server.review_db.exists():
                    cfg, _ = _read_optional_config(self.server.config_file)
                    if cfg and cfg.get("enabled") is True:
                        raise ApiError(503, "RSSPlus review database unavailable")
                    return self._json(200, {"items": [], "available": False})
                return self._json(200, {"items": _sqlite_read(self.server.review_db,
                    "SELECT id,chat_id,sub_id,status,created_at FROM items "
                    "ORDER BY created_at DESC LIMIT ?", (limit,)), "available": True})
            if path == "/v1/rules":
                cfg, etag = _read_optional_config(self.server.config_file)
                if cfg is None:
                    return self._json(200, {"enabled": False, "configured": False,
                                            "default": {}, "targets": {}})
                targets = cfg.get("targets", {})
                if not isinstance(targets, dict):
                    targets = {}
                public_targets = {str(k): _public_policy(v) for k, v in targets.items()
                                  if str(k).lstrip("-").isdigit()}
                return self._json(200, {"enabled": cfg.get("enabled") is True,
                    "configured": True, "default": _public_policy(cfg.get("default")),
                    "targets": public_targets}, etag)
            raise ApiError(404, "unknown endpoint")
        except ApiError as exc:
            self._json(exc.code, {"error": exc.message})
        except Exception:
            self._json(500, {"error": "internal error"})

    def do_POST(self):
        try:
            self._preflight()
            if self.path != "/v1/commands":
                raise ApiError(404, "unknown endpoint")
            if not self.server.allow_subscription_write:
                raise ApiError(403, "subscription commands disabled")
            body = self._strict_json()
            action = body.get("action")
            channel = body.get("channel_id")
            if not isinstance(action, str) or action not in NATIVE_ACTIONS:
                raise ApiError(400, "unsupported operation")
            if type(channel) is not int or channel not in self.server.allowed_channels:
                raise ApiError(403, "channel not approved")
            if action == "subscribe":
                if (set(body) != {"action", "channel_id", "url"}
                        or not isinstance(body.get("url"), str)
                        or not 8 <= len(body["url"]) <= 1024):
                    raise ApiError(400, "invalid subscription command")
            else:
                if (type(body.get("subscription_id")) is not int
                        or not 0 < body["subscription_id"] < 2**63):
                    raise ApiError(400, "invalid subscription command")
                if action in SETTING_ACTIONS:
                    if not _valid_setting_action(body):
                        raise ApiError(400, "invalid settings command")
                elif set(body) != {"action", "channel_id", "subscription_id"}:
                    raise ApiError(400, "invalid subscription command")
            sock = self.server.ipc_socket
            if sock.is_symlink() or not sock.is_socket():
                raise ApiError(503, "native bot control unavailable")
            event = "subscriptions." + action
            # A sanitised intent is recorded before any mutation; no URL or bearer in logs.
            self.server.audit(event + ".intent", "accepted", str(channel))
            conn = UnixHTTPConnection(sock)
            try:
                conn.request("POST", "/native/v1/command",
                             body=json.dumps(body, separators=(",", ":")).encode(),
                             headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                raw = response.read(2049)
                if len(raw) > 2048:
                    raise ApiError(502, "native response too large")
                try:
                    outcome = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ApiError(502, "native response invalid") from None
                if not isinstance(outcome, dict) or response.status not in (200, 400, 403, 404, 409, 503):
                    raise ApiError(502, "native response invalid")
                if response.status == 200 and outcome.get("success") is not True:
                    raise ApiError(502, "native response invalid")
                self.server.audit(event, "applied" if response.status == 200 else "rejected",
                                  str(channel))
                if response.status == 200:
                    output = {"success": True, "action": action, "channel_id": channel,
                              "subscription_id": outcome.get("subscription_id")}
                    if action in SETTING_ACTIONS:
                        expected_field = body["field"] if action == "set_option" else {
                            "set_title": "title", "set_hashtags": "tags",
                            "set_interval": "interval", "set_length_limit": "length_limit"
                        }[action]
                        if (outcome.get("field") != expected_field or
                                outcome.get("subscription_id") != body["subscription_id"]):
                            raise ApiError(502, "native settings response mismatch")
                        output["field"] = expected_field
                        output["value"] = outcome.get("value")
                    return self._json(200, output)
                return self._json(response.status, {"error": "native operation rejected"})
            except (OSError, http.client.HTTPException, TimeoutError):
                raise ApiError(503, "native bot control unavailable") from None
            finally:
                conn.close()
        except ApiError as exc:
            self.close_connection = True
            self._json(exc.code, {"error": exc.message})
        except Exception:
            self.close_connection = True
            self._json(500, {"error": "internal error"})

    def do_PUT(self):
        target = ""
        try:
            self._preflight()
            match = RULE_PATH.fullmatch(urlsplit(self.path).path)
            if match is None:
                raise ApiError(404, "unknown endpoint")
            if not self.server.allow_rules_write:
                raise ApiError(403, "rule updates disabled")
            target = str(int(match.group(1)))
            length_value = self.headers.get("Content-Length", "")
            if (not length_value.isdecimal() or not 1 <= int(length_value) <= MAX_REQUEST_BYTES or
                self.headers.get("Content-Type", "").split(";")[0] != "application/json" or
                self.headers.get("Transfer-Encoding")):
                raise ApiError(400, "invalid JSON request framing")
            try:
                body = json.loads(self.rfile.read(int(length_value)))
            except (ValueError, UnicodeError):
                raise ApiError(400, "invalid JSON") from None
            new_values = _validate_rule(body)
            etag = self.headers.get("If-Match", "")
            if not re.fullmatch(r'"[0-9a-f]{64}"', etag):
                raise ApiError(428, "If-Match ETag required")
            with WRITE_LOCK:
                config, current = _read_config(self.server.config_file)
                if current != etag:
                    raise ApiError(412, "config changed; refetch ETag")
                if config.get("enabled") is not True or not isinstance(config.get("targets", {}), dict):
                    raise ApiError(409, "RSSPlus not enabled")
                updated = dict(config.get("targets", {}).get(target, {}))
                updated.update(new_values)
                if updated.get("mode") == "review" and not config.get("review_chat_id"):
                    raise ApiError(409, "review_chat_id is not configured")
                config.setdefault("targets", {})[target] = updated
                # Fail closed if audit cannot be written. No rule update without an audit record.
                self.server.audit("rules.update.intent", "accepted", target)
                _atomic_json_write(self.server.config_file, config)
                self.server.audit("rules.update", "written", target)
                _, new_etag = _read_config(self.server.config_file)
            return self._json(200, {"updated": True, "target": target}, new_etag)
        except ApiError as exc:
            self._json(exc.code, {"error": exc.message})
        except Exception:
            self._json(500, {"error": "internal error"})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True,
                        help="Explicit RSStT SQLite DB path; never guessed by filename")
    parser.add_argument("--token-file", type=Path, required=True,
                        help="VPS-local restricted secret file, never in Git")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-rules-write", action="store_true", default=False)
    parser.add_argument("--allow-subscription-write", action="store_true", default=False)
    parser.add_argument("--allowed-channel", action="append", type=int, default=[],
                        help="Explicit authorized negative Telegram channel ID; repeat per channel")
    args = parser.parse_args(argv)
    config = args.config_dir.resolve(strict=True)
    if not config.is_dir() or args.config_dir.is_symlink():
        parser.error("config directory must be a real directory")
    if args.database.is_symlink():
        parser.error("SQLite database may not be a symlink")
    db = args.database.resolve(strict=True)
    if not db.is_file() or not db.is_relative_to(config):
        parser.error("SQLite database must be a regular file inside config")
    token_file = args.token_file
    if token_file.is_symlink() or not token_file.is_file() or (token_file.stat().st_mode & 0o077):
        parser.error("token file must be a restricted regular file, not a symlink")
    token = token_file.read_bytes().strip()
    if not 32 <= len(token) <= 256:
        parser.error("token must be 32 to 256 bytes")
    if not 1024 <= args.port <= 65535:
        parser.error("invalid port")
    if args.allow_subscription_write and (not args.allowed_channel
            or any(channel >= 0 for channel in args.allowed_channel)):
        parser.error("subscription writes require explicit negative --allowed-channel IDs")
    server = Controller(("127.0.0.1", args.port), config, db, token,
                        args.allow_rules_write, args.allow_subscription_write,
                        args.allowed_channel)
    print("KHABARCHIN_API=LOOPBACK_ONLY", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
