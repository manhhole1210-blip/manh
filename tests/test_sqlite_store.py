import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlite_store import SQLiteStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SQLiteStore(self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_migration_exact_backup_and_no_reimport(self):
        data = self.root / 'data'
        users = data / 'users.json'
        users.write_text('{"42":{"balance":123456,"language":"vi"}}')
        stock = data / 'accounts.txt'
        stock.write_bytes(b'user:password\r\n second \n')
        (self.root / 'autocrypto').mkdir()
        (self.root / 'autocrypto/config.json').write_text('{"api_key":"fixture-not-a-secret"}')
        backup = self.store.migrate()
        self.assertEqual((backup / 'data/users.json').read_bytes(), users.read_bytes())
        self.assertEqual((backup / 'data/accounts.txt').read_bytes(), stock.read_bytes())
        self.assertFalse((backup / 'autocrypto').exists())
        self.assertEqual(self.store.load_json(users, {})['42']['balance'], 123456)
        self.assertEqual(self.store.read_stock(stock), stock.read_bytes())
        self.store.save_json(users, {'42': {'balance': 7}})
        self.store.close()
        self.store = SQLiteStore(self.root)
        self.assertIsNone(self.store.migrate())
        self.assertEqual(self.store.load_json(users, {})['42']['balance'], 7)
        self.assertEqual(json.loads(users.read_text())['42']['balance'], 123456)

    def test_atomic_purchase_and_rollback(self):
        users = self.root / 'data/users.json'
        stock = self.root / 'data/accounts.txt'
        orders = self.root / 'data/orders.json'
        self.store.save_json(users, {'42': {'balance': 10000}})
        self.store.write_stock(stock, 'a\nb\n')
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.save_json(users, {'42': {'balance': 7500}})
                self.store.write_stock(stock, 'b\n')
                self.store.save_json(orders, {'LC-1': {'accounts': ['a']}})
                raise RuntimeError('simulated order failure')
        self.assertEqual(self.store.load_json(users, {})['42']['balance'], 10000)
        self.assertEqual(self.store.read_stock(stock), b'a\nb\n')
        self.assertFalse(self.store.exists(orders))
        with self.store.transaction():
            self.store.save_json(users, {'42': {'balance': 7500}})
            self.store.write_stock(stock, 'b\n')
            self.store.save_json(orders, {'LC-1': {'accounts': ['a']}})
        self.store.close()
        self.store = SQLiteStore(self.root)
        self.assertEqual(self.store.load_json(users, {})['42']['balance'], 7500)
        self.assertEqual(self.store.read_stock(stock), b'b\n')
        self.assertEqual(self.store.load_json(orders, {})['LC-1']['accounts'], ['a'])

    def test_corrupt_legacy_aborts_all_import(self):
        (self.root / 'data/users.json').write_text('{broken')
        with self.assertRaises(json.JSONDecodeError):
            self.store.migrate()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM documents').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM metadata').fetchone()[0], 0)

    def test_delete_does_not_resurrect_legacy_stock(self):
        stock = self.root / 'data/stock_1.txt'
        stock.write_text('a\n')
        self.store.migrate()
        self.store.delete(stock)
        self.store.migrate()
        self.assertEqual(self.store.read_stock(stock), b'')
        self.assertTrue(stock.exists())

    def test_backup_contains_committed_wal_and_is_independent(self):
        path = self.root / 'tickets.json'
        self.store.save_json(path, {'1': {'status': 'open'}})
        target = self.root / 'backups/check.db'
        self.store.backup(target)
        with sqlite3.connect(target) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            payload = db.execute('SELECT payload FROM documents WHERE path=?', ('tickets.json',)).fetchone()[0]
            self.assertEqual(json.loads(payload)['1']['status'], 'open')
        with self.assertRaises(ValueError):
            self.store.backup(target)

    def test_journal_requires_recovery(self):
        (self.root / 'data/transaction_journal.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'journal'):
            self.store.migrate()

    def test_import_stock_appends_and_rejects_duplicates(self):
        self.store.migrate()
        source = self.root / 'incoming.txt'
        source.write_text('a:password\nb:password\n')
        self.assertEqual(self.store.import_stock(source), 2)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.store.import_stock(source)
        self.assertEqual(self.store.read_stock(self.root / 'data/accounts.txt'), b'a:password\nb:password\n')
        with self.assertRaisesRegex(ValueError, 'Unknown product'):
            self.store.import_stock(source, 'missing')

    def test_processed_transaction_unique_and_rollback(self):
        path = self.root / 'data/transactions.json'
        self.store.save_json(path, ['old-id'])
        self.assertTrue(self.store.transaction_processed('old-id'))
        self.assertFalse(self.store.mark_transaction('old-id'))
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.assertTrue(self.store.mark_transaction('new-id'))
                raise RuntimeError('failed credit')
        self.assertFalse(self.store.transaction_processed('new-id'))
        self.assertEqual(self.store.load_json(path, []), ['old-id'])

    def test_wrong_ledger_shape_is_rejected(self):
        (self.root / 'data/users.json').write_text('[]')
        with self.assertRaisesRegex(ValueError, 'Invalid legacy'):
            self.store.migrate()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM metadata').fetchone()[0], 0)

    def test_outside_path_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save_json(self.root.parent / 'outside.json', {})


if __name__ == '__main__':
    unittest.main()
