#!/usr/bin/env python3
"""Local-only RSStT/RSSPlus SQLite online backup and isolated verification.

No files, tokens, database rows or secrets are uploaded to GitHub.
Standard library only. Run on the Docker host, not in GitHub Actions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SQLITE_MAGIC = b"SQLite format 3\x00"
SIDE_CAR_SUFFIXES = ("-wal", "-shm", "-journal")
SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")
CONTAINER_MATCH = re.compile(r"(rssplus|rsstt|rss[-_]to[-_]telegram)", re.I)


class BackupError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def docker(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["docker", *args], check=True, text=True, capture_output=True, timeout=30
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        raise BackupError("Docker inspection failed; no changes made") from exc
    return result.stdout


def identify_container(explicit: str | None) -> dict[str, Any]:
    if explicit:
        names = [explicit]
    else:
        names = docker(["ps", "--format", "{{.Names}}"]).splitlines()
        names = [name for name in names if CONTAINER_MATCH.search(name)]
    if len(names) != 1:
        raise BackupError(
            "Expected exactly one RSStT/RSSPlus container; specify --container to disambiguate"
        )
    info = json.loads(docker(["inspect", names[0]]))
    if len(info) != 1 or info[0].get("State", {}).get("Running") is not True:
        raise BackupError("Selected container is not running")
    return info[0]


def container_state(info: dict[str, Any]) -> tuple[Path, list[Path]]:
    mounts = [m for m in info.get("Mounts", []) if m.get("Destination") == "/app/config"]
    if len(mounts) != 1 or mounts[0].get("Type") not in {"bind", "volume"}:
        raise BackupError("Expected one persistent bind/volume mount at /app/config")
    config = Path(mounts[0]["Source"]).resolve(strict=True)
    if not config.is_dir():
        raise BackupError("Persistent config mount is not an accessible directory")

    # Container inspect exposes environment variables, but values must never be logged.
    env = info.get("Config", {}).get("Env") or []
    db_values = [entry.partition("=")[2] for entry in env if entry.startswith("DATABASE_URL=")]
    if db_values:
        db_url = db_values[-1].lower()
        if not db_url.startswith("sqlite:"):
            raise BackupError("Non-SQLite DATABASE_URL: this tool cannot claim a complete backup")
        # Common accepted forms: sqlite:/app/config/db.sqlite3 and
        # SQLAlchemy-style sqlite:////app/config/db.sqlite3. Fail closed on
        # other explicit locations (including /app path outside the mount).
        db_location = db_url.removeprefix("sqlite:")
        if db_location.startswith("////"):
            db_location = db_location[3:]
        if not db_location.startswith("/app/config/"):
            raise BackupError("Explicit SQLite database location is outside /app/config or ambiguous")

    labels = info.get("Config", {}).get("Labels") or {}
    working = labels.get("com.docker.compose.project.working_dir", "")
    compose_files = labels.get("com.docker.compose.project.config_files", "")
    paths: list[Path] = []
    if working:
        work = Path(working).resolve(strict=False)
        if work.is_dir():
            for name in (".env", ".env.production", "Dockerfile", "docker-compose.yml",
                         "docker-compose.yaml", "compose.yml", "compose.yaml",
                         "docker-compose.override.yml", "compose.override.yml"):
                p = work / name
                if p.is_file() or p.is_symlink():
                    paths.append(p)
    for entry in compose_files.split(","):
        entry = entry.strip()
        if entry:
            p = Path(entry)
            if p.is_file() or p.is_symlink():
                paths.append(p)
            else:
                raise BackupError("Compose label references an unavailable file")
    return config, list(dict.fromkeys(paths))


def inventory(config: Path) -> tuple[list[Path], list[Path]]:
    if config.is_symlink():
        raise BackupError("Config root may not be a symbolic link")
    dbs: list[Path] = []
    regular: list[Path] = []
    for directory, dirs, files in os.walk(config, followlinks=False):
        for d in dirs:
            if (Path(directory) / d).is_symlink():
                raise BackupError("Symlinked directory in config; refuse incomplete snapshot")
        for name in files:
            p = Path(directory) / name
            if p.is_symlink():
                raise BackupError("Symlinked file in config; refuse incomplete snapshot")
            st = p.stat()
            if not stat.S_ISREG(st.st_mode):
                raise BackupError("Nonregular file in config; refuse potentially unsafe snapshot")
            if name.lower().endswith(SIDE_CAR_SUFFIXES):
                continue  # Online SQLite backup supersedes WAL/journal sidecars.
            with p.open("rb") as stream:
                signature = stream.read(len(SQLITE_MAGIC))
            if signature == SQLITE_MAGIC:
                dbs.append(p)
            elif p.suffix.lower() in SQLITE_SUFFIXES:
                raise BackupError("Database-looking file has an invalid SQLite header")
            else:
                regular.append(p)
    return sorted(dbs), sorted(regular)


def _backing_fs(path: Path, mountinfo: str) -> str:
    """Find the filesystem type of the longest matching Linux mount."""
    selected = None
    length = -1
    for line in mountinfo.splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields = left.split()
        details = right.split()
        if len(fields) < 5 or not details:
            continue
        # Linux mountinfo escapes spaces and backslashes in mount point paths.
        mount = fields[4].replace("\\040", " ").replace("\\134", "\\")
        mountpath = Path(mount)
        if (path == mountpath or mountpath in path.parents) and len(mount) > length:
            selected = details[0].lower()
            length = len(mount)
    if selected is None:
        raise BackupError("Unable to verify destination filesystem")
    return selected


def _assert_local_storage(path: Path) -> None:
    """Refuse known network/remote and volatile mounts, not merely remote URLs."""
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError as exc:
        raise BackupError("Cannot inspect local mount information") from exc
    fs = _backing_fs(path.resolve(strict=True), mountinfo)
    disallowed = {
        "nfs", "nfs4", "cifs", "smb3", "9p", "ceph", "glusterfs",
        "lustre", "davfs", "sshfs", "fuse.sshfs", "fuse.rclone",
        "fuse.s3fs", "fuse.gcsfuse", "fuse.goofys", "fuse.davfs",
        "tmpfs", "ramfs", "devtmpfs",
    }
    if fs in disallowed or fs.startswith("fuse.") or fs.startswith("nfs"):
        raise BackupError("Backup destination is not confirmed local persistent storage")


def _private_file_copy(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise BackupError("Deployment file is missing or symlinked")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with source.open("rb") as original, target.open("xb") as copied:
        os.chmod(target, 0o600)
        shutil.copyfileobj(original, copied)


def _db_check(db: Path) -> None:
    # A separate copied database is tested; the original is never modified.
    source = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        result = source.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise BackupError("SQLite integrity check failed for saved snapshot")
        source.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
    finally:
        source.close()


def _online_sqlite_backup(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    origin = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=10)
    output = sqlite3.connect(target_path)
    try:
        os.chmod(target_path, 0o600)
        origin.backup(output, pages=200, sleep=0.1)
    finally:
        output.close()
        origin.close()
    _db_check(target_path)


def _record_file(root: Path, file: Path, kind: str) -> dict[str, Any]:
    return {
        "path": file.relative_to(root).as_posix(),
        "kind": kind,
        "bytes": file.stat().st_size,
        "sha256": sha256(file),
    }


def create_backup(config: Path, deployment_files: list[Path], backup_root: Path,
                  expected_db_count: int | None = None) -> Path:
    if config.is_symlink():
        raise BackupError("Live config root must not be a symbolic link")
    config = config.resolve(strict=True)
    backup_root = backup_root.expanduser().absolute()
    for part in (backup_root, *backup_root.parents):
        if part.is_symlink():
            raise BackupError("Backup root and its parents must not be symbolic links")
    backup_root = backup_root.resolve(strict=False)
    if backup_root == config or config in backup_root.parents:
        raise BackupError("Backup destination may not be within the live config directory")

    dbs, others = inventory(config)
    if not dbs:
        raise BackupError("No SQLite database found; backup not started")
    if expected_db_count is not None and len(dbs) != expected_db_count:
        raise BackupError("SQLite database count differs from --expect-databases; aborting")

    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backup_root, 0o700)
    _assert_local_storage(backup_root)
    tmp = Path(tempfile.mkdtemp(prefix=".rsstt-incomplete-", dir=backup_root))
    os.chmod(tmp, 0o700)
    records: list[dict[str, Any]] = []
    try:
        for path in dbs:
            target = tmp / "sqlite" / path.relative_to(config)
            _online_sqlite_backup(path, target)
            records.append(_record_file(tmp, target, "sqlite-online-backup"))
        for path in others:
            target = tmp / "state" / "config" / path.relative_to(config)
            _private_file_copy(path, target)
            records.append(_record_file(tmp, target, "config"))
        # No filenames/values of secret-bearing data are printed to stdout.
        deployment_names = set()
        for source in deployment_files:
            key = source.name
            if key in deployment_names:
                raise BackupError("Ambiguous deployment filenames; refuse partial state archive")
            deployment_names.add(key)
            target = tmp / "state" / "deployment" / key
            _private_file_copy(source, target)
            records.append(_record_file(tmp, target, "deployment"))
        manifest = {
            "format": "khabarchin-rsstt-backup-v1",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "database_count": len(dbs),
            "config_file_count": len(others),
            "deployment_file_count": len(deployment_files),
            "files": records,
        }
        manifest_path = tmp / "manifest.json"
        with manifest_path.open("x", encoding="utf-8") as file:
            os.chmod(manifest_path, 0o600)
            json.dump(manifest, file, indent=2, ensure_ascii=False)
            file.write("\n")
        verify_backup(tmp)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # Atomic publication: incomplete runs remain .rsstt-incomplete-* and aren't mistaken for success.
        name = f"rsstt-{timestamp}-{os.urandom(4).hex()}"
        completed = backup_root / name
        tmp.rename(completed)
        return completed
    except BaseException:
        # Sensitive incomplete data should never be left in the finished backup directory.
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def verify_backup(backup_dir: Path) -> dict[str, int]:
    root = backup_dir.resolve(strict=True)
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink():
        raise BackupError("Manifest symlink is forbidden")
    with manifest_path.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    if manifest.get("format") != "khabarchin-rsstt-backup-v1":
        raise BackupError("Unsupported backup format")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise BackupError("Malformed backup manifest")
    db_count = 0
    for record in records:
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise BackupError("Manifest contains unsafe file path")
        target = root / relative
        if not target.is_file() or target.is_symlink():
            raise BackupError("A saved file is missing or has been replaced with a symlink")
        if target.stat().st_size != record["bytes"] or sha256(target) != record["sha256"]:
            raise BackupError("Saved file checksum mismatch")
        if record["kind"] == "sqlite-online-backup":
            db_count += 1
            with tempfile.TemporaryDirectory(prefix="rsstt-verify-") as tmp:
                independent = Path(tmp) / "restored.sqlite3"
                shutil.copyfile(target, independent)
                _db_check(independent)
    if db_count != manifest.get("database_count") or db_count < 1:
        raise BackupError("Database count mismatch")
    if len(records) != (manifest.get("database_count", 0) +
                        manifest.get("config_file_count", 0) +
                        manifest.get("deployment_file_count", 0)):
        raise BackupError("Manifest file count mismatch")
    return {"databases": db_count, "files": len(records)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup", help="Back up all SQLite DBs and config to the Docker host")
    b.add_argument("--container", help="Running Docker container name (e.g. rssplus)")
    b.add_argument("--backup-root", default="/var/backups/khabarchin/rsstt")
    b.add_argument("--expect-databases", type=int)
    v = sub.add_parser("verify", help="Checksum and independently restore-test a local backup")
    v.add_argument("backup_dir")
    a = sub.add_parser("audit", help="Show only counts, without reading DB records")
    a.add_argument("--container")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "verify":
            summary = verify_backup(Path(args.backup_dir))
            print(f"PASS: {summary['databases']} SQLite snapshots, {summary['files']} files verified")
        else:
            info = identify_container(args.container)
            config, deployment_files = container_state(info)
            dbs, regular = inventory(config)
            print(f"Discovered: {len(dbs)} SQLite DB(s), {len(regular)} other config file(s), "
                  f"{len(deployment_files)} deployment file(s)")
            if args.command == "backup":
                if not deployment_files:
                    raise BackupError("Compose deployment files were not discovered; refuse incomplete backup")
                result = create_backup(config, deployment_files, Path(args.backup_root),
                                       expected_db_count=args.expect_databases)
                print(f"PASS: local backup and independent restore verification complete: {result}")
            else:
                print("AUDIT ONLY: no backup written; no service changed")
        return 0
    except (BackupError, OSError, sqlite3.Error, ValueError, KeyError, json.JSONDecodeError) as exc:
        # Do not emit untrusted error detail; paths and credentials may appear in OS/SQLite errors.
        print(f"FAIL: {type(exc).__name__}; no production restore or restart attempted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
