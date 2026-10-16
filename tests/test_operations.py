import json
import sqlite3
import tempfile
import unittest
import time
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

from money import vnd, crypto_to_vnd
from operations import RuntimePolicy, rotating_backup, restore_database, readonly_database
from run_bot import heartbeat_stale, restart_delay
from sqlite_store import SQLiteStore, acquire_process_lock


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SQLiteStore(self.root)
        self.store.migrate()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_exact_money_and_fraction_rejection(self):
        self.assertEqual(vnd('9000000000000000'), 9000000000000000)
        self.assertEqual(vnd(2500.0), 2500)
        for invalid in (True, -1, '1.5', 'NaN', 'Infinity', '9000000000000001'):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                vnd(invalid)
        self.assertEqual(crypto_to_vnd('0.00002', '25000'), 1)
        self.assertEqual(crypto_to_vnd('2.005', '25000'), 50125)

    def test_integer_money_upgrade_has_backup_and_is_repeatable(self):
        path = self.root / 'data/users.json'
        self.store.save_json(path, {'42': {'balance': 12345.0}})
        self.store.upgrade_vnd()
        value = self.store.load_json(path, {})['42']['balance']
        self.assertIs(type(value), int)
        self.assertEqual(value, 12345)
        self.assertEqual(len(list((self.root / 'backups').glob('pre-integer-vnd-*.db'))), 1)
        self.store.upgrade_vnd()
        self.assertEqual(len(list((self.root / 'backups').glob('pre-integer-vnd-*.db'))), 1)

    def test_fractional_legacy_amount_does_not_partially_upgrade(self):
        path = self.root / 'data/users.json'
        self.store.save_json(path, {'42': {'balance': 12345.5}})
        with self.assertRaisesRegex(ValueError, 'No balances were changed'):
            self.store.upgrade_vnd()
        self.assertEqual(self.store.load_json(path, {})['42']['balance'], 12345.5)
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM metadata WHERE key='integer_vnd_v1'").fetchone())

    def test_policy_requires_guild_id_admin_id_and_permission(self):
        policy = RuntimePolicy(self.root)
        policy.config.update(allowed_guild_ids=[123], admin_user_ids=[42])
        interaction = SimpleNamespace(guild=SimpleNamespace(id=123), user=SimpleNamespace(id=42), permissions=SimpleNamespace(administrator=True))
        self.assertTrue(policy.can_manage(interaction))
        interaction.guild.id = 999
        self.assertFalse(policy.can_manage(interaction))
        interaction.guild.id = 123
        interaction.user.id = 99
        self.assertFalse(policy.can_manage(interaction))
        interaction.user.id = 42
        interaction.permissions.administrator = False
        self.assertFalse(policy.can_manage(interaction))
        interaction.guild = None
        self.assertFalse(policy.can_manage(interaction))

    def test_rotating_backups_preserve_wal_and_only_prune_owned_files(self):
        path = self.root / 'data/users.json'
        self.store.save_json(path, {'42': {'balance': 500}})
        folder = self.root / 'backups/sqlite'
        folder.mkdir(parents=True)
        untouched = folder / 'manual-save.db'
        untouched.write_bytes(b'keep')
        for balance in (600, 700, 800):
            self.store.save_json(path, {'42': {'balance': balance}})
            target = rotating_backup(self.root, keep=2)
        self.assertEqual(len(list(folder.glob('bot-*.db'))), 2)
        self.assertEqual(untouched.read_bytes(), b'keep')
        db = readonly_database(target)
        try:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(json.loads(db.execute("SELECT payload FROM users WHERE id='42'").fetchone()[0])['balance'], 800)
        finally:
            db.close()

    def test_restore_roundtrip_preserves_current_database_and_pauses_payments(self):
        path = self.root / 'data/users.json'
        self.store.save_json(path, {'42': {'balance': 1000}})
        backup = rotating_backup(self.root)
        self.store.save_json(path, {'42': {'balance': 2000}})
        self.store.close()
        preserved = restore_database(self.root, backup)
        self.store = SQLiteStore(self.root)
        self.assertEqual(self.store.load_json(path, {})['42']['balance'], 1000)
        self.assertTrue(self.store.payments_paused())
        db = readonly_database(preserved)
        try:
            self.assertEqual(json.loads(db.execute("SELECT payload FROM users WHERE id='42'").fetchone()[0])['balance'], 2000)
        finally:
            db.close()

    def test_restore_refuses_running_bot_and_invalid_database(self):
        target = rotating_backup(self.root)
        lock = acquire_process_lock(self.root / 'data')
        try:
            with self.assertRaises(OSError):
                restore_database(self.root, target)
        finally:
            lock.close()
        other = self.root / 'unrelated.db'
        sqlite3.connect(other).close()
        with self.assertRaisesRegex(ValueError, 'compatible'):
            restore_database(self.root, other)
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM metadata WHERE key='legacy_import_v1'").fetchone())

    def test_supervisor_backoff_and_heartbeat(self):
        self.assertEqual([restart_delay(n) for n in range(1, 6)], [5, 10, 20, 40, 60])
        self.assertFalse(heartbeat_stale(100, 100, 399))
        self.assertTrue(heartbeat_stale(100, 100, 401))
        self.assertFalse(heartbeat_stale(100, 390, 401))

    def test_supervisor_actually_restarts_failed_child_then_exits_cleanly(self):
        import run_bot
        script = self.root / 'main.py'
        script.write_text("from pathlib import Path\nimport sys\np=Path('counter')\nn=int(p.read_text())+1 if p.exists() else 1\np.write_text(str(n))\nprint('fixture child',n)\nsys.exit(1 if n==1 else 0)\n")
        clock = SimpleNamespace(time=time.time, sleep=lambda seconds: time.sleep(min(seconds, 0.01)))
        with patch.object(run_bot, '__file__', str(self.root / 'run_bot.py')), patch.object(run_bot, 'time', clock):
            self.assertEqual(run_bot.main(), 0)
        self.assertEqual((self.root / 'counter').read_text(), '2')
        self.assertIn('restarting', (self.root / 'logs/bot.log').read_text())


if __name__ == '__main__':
    unittest.main()
