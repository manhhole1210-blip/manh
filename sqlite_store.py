"""Transactional SQLite persistence; legacy files are read only during migration."""
import argparse
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class SQLiteStore:
    # Keep each customer/order separately queryable while preserving legacy fields.
    ENTITY_TABLES = {
        'data/users.json': 'users',
        'data/orders.json': 'orders',
        'data/crypto_orders.json': 'crypto_orders',
    }
    def __init__(self, root):
        self.root = Path(root).resolve()
        (self.root / 'data').mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'data' / 'bot.db'
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        os.chmod(self.path, 0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents (path TEXT PRIMARY KEY, payload TEXT NOT NULL CHECK(json_valid(payload)));
            CREATE TABLE IF NOT EXISTS processed_transactions (transaction_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS stock (path TEXT PRIMARY KEY, content BLOB NOT NULL);
        ''')
        for table in self.ENTITY_TABLES.values():
            self.db.execute(f'CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, payload TEXT NOT NULL CHECK(json_valid(payload)))')

    def key(self, path):
        return Path(path).resolve().relative_to(self.root).as_posix()

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            raise RuntimeError('Nested SQLite transaction')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def exists(self, path):
        key = self.key(path)
        return self.db.execute('SELECT 1 FROM documents WHERE path=? UNION ALL SELECT 1 FROM stock WHERE path=?', (key, key)).fetchone() is not None

    def save_json(self, path, value):
        key = self.key(path)
        table = self.ENTITY_TABLES.get(key)
        # Serialize first; invalid values must not partially replace a collection.
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        transaction_list = key == 'data/transactions.json' and isinstance(value, list) and all(isinstance(item, str) for item in value)
        def write():
            if key == 'data/transactions.json':
                self.db.execute('DELETE FROM processed_transactions')
                if transaction_list:
                    self.db.executemany('INSERT OR IGNORE INTO processed_transactions VALUES (?)', [(item,) for item in value])
            if table:
                self.db.execute(f'DELETE FROM {table}')
                if isinstance(value, dict):
                    self.db.executemany(f'INSERT INTO {table} VALUES (?,?)',
                        [(str(k), json.dumps(v, ensure_ascii=False, allow_nan=False)) for k, v in value.items()])
            self.db.execute('INSERT INTO documents VALUES (?,?) ON CONFLICT(path) DO UPDATE SET payload=excluded.payload',
                (key, '[]' if transaction_list else '{}' if table and isinstance(value, dict) else payload))
        if self.db.in_transaction:
            write()
        else:
            with self.transaction():
                write()

    def load_json(self, path, default):
        key = self.key(path)
        row = self.db.execute('SELECT payload FROM documents WHERE path=?', (key,)).fetchone()
        if row is None:
            self.save_json(path, default)
            return json.loads(json.dumps(default))
        table = self.ENTITY_TABLES.get(key)
        value = json.loads(row[0])
        if key == 'data/transactions.json' and isinstance(value, list) and not value:
            return [item[0] for item in self.db.execute('SELECT transaction_id FROM processed_transactions ORDER BY rowid')]
        if table and isinstance(value, dict):
            return {item[0]: json.loads(item[1]) for item in self.db.execute(f'SELECT id, payload FROM {table} ORDER BY rowid')}
        return value

    def transaction_processed(self, transaction_id):
        return self.db.execute('SELECT 1 FROM processed_transactions WHERE transaction_id=?', (str(transaction_id).strip(),)).fetchone() is not None

    def mark_transaction(self, transaction_id):
        transaction_id = str(transaction_id).strip()
        if not transaction_id:
            raise ValueError('Empty transaction ID')
        def write():
            return self.db.execute('INSERT OR IGNORE INTO processed_transactions VALUES (?)', (transaction_id,)).rowcount == 1
        if self.db.in_transaction:
            return write()
        with self.transaction():
            return write()

    def read_stock(self, path):
        row = self.db.execute('SELECT content FROM stock WHERE path=?', (self.key(path),)).fetchone()
        return bytes(row[0]) if row else b''

    def write_stock(self, path, content):
        if isinstance(content, str):
            content = content.encode('utf-8')
        self.db.execute('INSERT INTO stock VALUES (?,?) ON CONFLICT(path) DO UPDATE SET content=excluded.content', (self.key(path), content))

    def delete(self, path):
        key = self.key(path)
        def write():
            table = self.ENTITY_TABLES.get(key)
            if table:
                self.db.execute(f'DELETE FROM {table}')
            if key == 'data/transactions.json':
                self.db.execute('DELETE FROM processed_transactions')
            self.db.execute('DELETE FROM documents WHERE path=?', (key,))
            self.db.execute('DELETE FROM stock WHERE path=?', (key,))
        if self.db.in_transaction:
            write()
        else:
            with self.transaction():
                write()

    def migrate(self):
        if self.db.execute("SELECT 1 FROM metadata WHERE key='legacy_import_v1'").fetchone():
            return None
        if (self.root / 'data' / 'transaction_journal.json').exists():
            raise RuntimeError('Legacy journal exists: start main.py to recover it before migrating')
        data = self.root / 'data'
        paths = list(data.glob('*.json')) + list((data / 'orders').glob('*.json'))
        paths += list(data.glob('stock_*.txt'))
        paths += [data / 'accounts.txt', self.root / 'tickets.json', self.root / 'user_languages.json', self.root / 'config.json']
        paths = sorted({p for p in paths if p.is_file()})
        # Validate all data before importing anything; never silently reset corrupt ledgers.
        decoded = {}
        for path in paths:
            self.key(path)  # Reject symlinks that escape the application directory.
            decoded[path] = json.loads(path.read_text(encoding='utf-8')) if path.suffix == '.json' else path.read_bytes()
            key = self.key(path)
            expected = {
                'data/users.json': dict, 'data/orders.json': dict,
                'data/crypto_orders.json': dict, 'data/products.json': dict,
                'data/topup_history.json': list, 'data/transactions.json': (list, dict),
            }.get(key)
            if expected and not isinstance(decoded[path], expected):
                raise ValueError(f'Invalid legacy data structure in {key}; restore or reconcile it before migration')
        backup = None
        if paths:
            backup = self.root / 'backups' / ('legacy-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            backup.mkdir(parents=True, mode=0o700)
            for path in paths:
                target = backup / path.relative_to(self.root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                os.chmod(target, 0o600)
        with self.transaction():
            for path, value in decoded.items():
                if path.suffix == '.json':
                    self.save_json(path, value)
                else:
                    self.write_stock(path, value)
            self.db.execute("INSERT INTO metadata VALUES ('legacy_import_v1', ?)", (datetime.now().isoformat(),))
        return backup

    def import_stock(self, source, product_id='default'):
        lines = [line.strip() for line in Path(source).read_text(encoding='utf-8-sig').splitlines() if line.strip()]
        if not lines:
            raise ValueError('The account file is empty')
        if len(set(lines)) != len(lines):
            raise ValueError('The import file contains duplicate accounts')
        with self.transaction():
            path = self.root / 'data/accounts.txt'
            if product_id != 'default':
                catalog = self.load_json(self.root / 'data/products.json', {'products': []})
                product = next((p for p in catalog['products'] if str(p['id']) == product_id), None)
                if product is None:
                    raise ValueError('Unknown product ID')
                filename = product['stock_file']
                if filename != Path(filename).name or not filename.startswith('stock_') or not filename.endswith('.txt'):
                    raise ValueError('Invalid product stock filename')
                path = self.root / 'data' / filename
            existing = [line.strip() for line in self.read_stock(path).decode('utf-8').splitlines() if line.strip()]
            if set(existing).intersection(lines):
                raise ValueError('An account already exists in this product stock')
            self.write_stock(path, ''.join(line + '\n' for line in existing + lines))
        return len(lines)

    def backup(self, target):
        target = Path(target).resolve()
        if target.exists() or target == self.path:
            raise ValueError('Choose a new backup filename')
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(target) as dest:
            self.db.backup(dest)
        os.chmod(target, 0o600)

    def close(self):
        self.db.close()


def main():
    parser = argparse.ArgumentParser(description='Migrate, back up, or replenish bot SQLite data')
    parser.add_argument('--root', type=Path, default=Path(__file__).parent)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--backup', type=Path)
    action.add_argument('--import-stock', type=Path, help='Append a UTF-8 account file; stop the bot first')
    parser.add_argument('--product', default='default', help='Product ID for stock import, e.g. default or p1')
    args = parser.parse_args()
    # Mutating maintenance must not race the running bot's in-memory ledger.
    lock = None
    if not args.backup:
        import fcntl
        (args.root / 'data').mkdir(parents=True, exist_ok=True)
        lock = open(args.root / 'data/.bot.lock', 'a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            parser.error('Stop the bot before migration or stock import')
    if args.backup and not (args.root / 'data/bot.db').is_file():
        parser.error('No existing database to back up')
    store = SQLiteStore(args.root)
    try:
        if args.backup:
            store.backup(args.backup)
            print('SQLite backup completed')
        else:
            backup = store.migrate()
            print('SQLite migration complete; legacy backup:', backup or 'not needed/already migrated')
            if args.import_stock:
                count = store.import_stock(args.import_stock, args.product)
                print(f'Added {count} accounts to product {args.product}')
        print('Integrity:', store.db.execute('PRAGMA integrity_check').fetchone()[0])
    finally:
        store.close()
        if lock:
            lock.close()


if __name__ == '__main__':
    main()
