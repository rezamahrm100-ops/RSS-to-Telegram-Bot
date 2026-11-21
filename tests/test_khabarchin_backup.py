"""Offline, synthetic-data checks for the VPS-local backup utility."""
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "rsstt_backup.py"
spec = importlib.util.spec_from_file_location("khabarchin_rsstt_backup", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "live_config"
        self.config.mkdir()
        self.deployment = self.root / "deployment"
        self.deployment.mkdir()
        (self.config / "rssplus.json").write_text('{"review_chat_id":123}\n')
        (self.deployment / ".env").write_text("TOKEN=FAKE_DO_NOT_UPLOAD\n")
        for name in ("active.db", "old.sqlite3"):
            db = sqlite3.connect(self.config / name)
            db.execute("CREATE TABLE example(id INTEGER PRIMARY KEY, content TEXT)")
            db.execute("INSERT INTO example(content) VALUES ('example')")
            db.commit()
            db.close()

    def test_backup_and_isolated_restore_two_databases(self):
        result = mod.create_backup(self.config, [self.deployment / ".env"],
                                   self.root / "backups", expected_db_count=2)
        summary = mod.verify_backup(result)
        self.assertEqual(summary["databases"], 2)
        self.assertEqual(summary["files"], 4)
        self.assertIn("FAKE_DO_NOT_UPLOAD", (result / "state/deployment/.env").read_text())
        db = sqlite3.connect(result / "sqlite/active.db")
        self.assertEqual(db.execute("SELECT content FROM example").fetchone()[0], "example")
        db.close()
        (self.config / "active.db").unlink()
        self.assertEqual(mod.verify_backup(result)["databases"], 2)

    def test_wal_is_snapshotted_without_copying_sidecars(self):
        database = self.config / "active.db"
        live = sqlite3.connect(database)
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("INSERT INTO example(content) VALUES ('WAL row')")
        live.commit()
        output = mod.create_backup(self.config, [], self.root / "backups")
        self.assertFalse((output / "state/config/active.db-wal").exists())
        snapshot = sqlite3.connect(output / "sqlite/active.db")
        self.assertEqual(snapshot.execute("SELECT COUNT(*) FROM example").fetchone()[0], 2)
        snapshot.close()
        live.close()

    def test_refuses_database_count_mismatch_without_partial_snapshot(self):
        with self.assertRaises(mod.BackupError):
            mod.create_backup(self.config, [], self.root / "backups", expected_db_count=3)
        self.assertFalse((self.root / "backups").exists())

    def test_tamper_detected(self):
        output = mod.create_backup(self.config, [], self.root / "backups")
        with (output / "state/config/rssplus.json").open("a") as stream:
            stream.write("tampered")
        with self.assertRaises(mod.BackupError):
            mod.verify_backup(output)

    def test_rejects_backup_under_live_config(self):
        with self.assertRaises(mod.BackupError):
            mod.create_backup(self.config, [], self.config / "backups")

    def test_rejects_symlinks_in_config(self):
        (self.config / "link.txt").symlink_to(self.deployment / ".env")
        with self.assertRaises(mod.BackupError):
            mod.create_backup(self.config, [], self.root / "backups")

    def test_explicit_db_url_must_match_live_volume(self):
        mod._validate_sqlite_config_url("sqlite:/app/config/active.db", self.config)
        mod._validate_sqlite_config_url("sqlite:////app/config/old.sqlite3", self.config)
        with self.assertRaises(mod.BackupError):
            mod._validate_sqlite_config_url("sqlite:/app/config/missing.sqlite3", self.config)

    def test_dotenv_overrides_must_not_point_to_external_database(self):
        envfile = self.config / ".env"
        envfile.write_text("TOKEN=FAKE\\nDATABASE_URL=postgres://user:fake@remote/db\\n")
        with self.assertRaises(mod.BackupError):
            mod._check_local_dotenv(envfile, self.config)
        envfile.write_text("DATABASE_URL=sqlite:/outside/config.db\\n")
        with self.assertRaises(mod.BackupError):
            mod._check_local_dotenv(envfile, self.config)

    def test_mountinfo_selects_longest_matching_mount(self):
        table = ("1 0 0:1 / / rw - ext4 /dev/vda1 rw\n"
                 "2 1 0:2 / /var/backups rw - nfs4 server:/archive rw\n")
        self.assertEqual(mod._backing_fs(Path("/var/backups/khabarchin"), table), "nfs4")
        self.assertEqual(mod._backing_fs(Path("/srv/khabarchin"), table), "ext4")

    def test_mountinfo_rejects_unverifiable_location(self):
        with self.assertRaises(mod.BackupError):
            mod._backing_fs(Path("/var/backups/khabarchin"), "")

    def test_rejects_symlinked_backup_root(self):
        backing = self.root / "actual_output"
        backing.mkdir()
        shortcut = self.root / "shortcut"
        shortcut.symlink_to(backing, target_is_directory=True)
        with self.assertRaises(mod.BackupError):
            mod.create_backup(self.config, [], shortcut)

    def test_rejects_symlinked_backup_ancestor(self):
        backing = self.root / "actual_output"
        backing.mkdir()
        shortcut = self.root / "shortcut"
        shortcut.symlink_to(backing, target_is_directory=True)
        with self.assertRaises(mod.BackupError):
            mod.create_backup(self.config, [], shortcut / "nested")


if __name__ == "__main__":
    unittest.main()
