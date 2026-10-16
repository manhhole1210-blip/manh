"""Transactional SQLite persistence; legacy files are read only during migration."""
import argparse
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


def read_legacy_json(path):
    """Read Windows UTF-8/BOM exports; errors identify files, never print contents."""
    path = Path(path)
    try:
        text = path.read_text(encoding='utf-8-sig')
    except UnicodeError:
        raise ValueError(f'Invalid UTF-8 encoding in {path}; save a copy as UTF-8 before retrying') from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise json.JSONDecodeError(
            f'Invalid JSON in {path}: {exc.msg}. Back up and repair this file; no legacy data was imported',
            exc.doc, exc.pos) from None


def acquire_process_lock(data_dir):
    """Hold the returned file open to lock one bot/maintenance process."""
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(directory / '.bot.lock', 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    return handle


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
            CREATE TABLE IF NOT EXISTS payment_claims (provider TEXT, payment_id TEXT, PRIMARY KEY(provider,payment_id));
            CREATE TABLE IF NOT EXISTS bank_payments (
                transaction_id TEXT PRIMARY KEY, amount_vnd INTEGER NOT NULL CHECK(amount_vnd>0),
                description TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
                user_id TEXT, actor_id TEXT, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
                order_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT, message_id TEXT);
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
            decoded[path] = read_legacy_json(path) if path.suffix == '.json' else path.read_bytes()
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

    def upgrade_vnd(self):
        from money import vnd
        if self.db.execute("SELECT 1 FROM metadata WHERE key='integer_vnd_v1'").fetchone():
            return
        def row_money(row, fields):
            if not isinstance(row, dict):
                raise ValueError('Expected a financial record')
            updated = dict(row)
            for field in fields:
                if field in updated:
                    updated[field] = vnd(updated[field])
            return updated

        def tier_prices(tiers):
            if not isinstance(tiers, list):
                raise ValueError('Expected a price-tier list')
            return [row_money(tier, ('price',)) for tier in tiers]

        changes = []
        for key, payload in self.db.execute('SELECT path, payload FROM documents').fetchall():
            # Only live financial schemas are authoritative. Audit trails, top-up
            # history and old notification payloads are immutable historical evidence;
            # a field named "total" there is not necessarily a current VND balance.
            selected = key in {
                'data/users.json', 'data/orders.json', 'data/crypto_orders.json',
                'data/products.json', 'data/prices.json', 'data/config.json',
            } or (key.startswith('data/orders/') and key.endswith('.json'))
            if not selected:
                continue
            value = self.load_json(self.root / key, None)
            try:
                if key in ('data/users.json', 'data/orders.json', 'data/crypto_orders.json'):
                    if not isinstance(value, dict):
                        raise ValueError('Expected a financial collection')
                    fields = {'data/users.json': ('balance',),
                              'data/orders.json': ('unit_price', 'total'),
                              'data/crypto_orders.json': ('credit_vnd',)}[key]
                    updated = {item_id: row_money(row, fields) for item_id, row in value.items()}
                elif key.startswith('data/orders/'):
                    updated = row_money(value, ('unit_price', 'total'))
                elif key == 'data/products.json':
                    updated = row_money(value, ())
                    products = value.get('products', [])
                    if not isinstance(products, list):
                        raise ValueError('Expected a product list')
                    updated['products'] = []
                    for product in products:
                        product = row_money(product, ('price',))
                        if 'tiers' in product:
                            product['tiers'] = tier_prices(product['tiers'])
                        updated['products'].append(product)
                else:
                    updated = row_money(value, ('price_tier_1', 'price_tier_2', 'price_tier_3', 'price_tier_4'))
                    if key == 'data/prices.json' and 'tiers' in updated:
                        updated['tiers'] = tier_prices(updated['tiers'])
                changes.append((self.root / key, updated))
            except ValueError:
                hint = ' Run: python manage_bot.py check-money.' if key == 'data/users.json' else ''
                raise ValueError(f'Invalid/fractional VND in {key}; reconcile it before upgrading. No balances were changed.' + hint) from None
        from operations import snapshot_database, utc_stamp
        snapshot_database(self.path, self.root / 'backups' / f'pre-integer-vnd-{utc_stamp()}.db')
        with self.transaction():
            for path, value in changes:
                self.save_json(path, value)
            for order in self.load_json(self.root / 'data/crypto_orders.json', {}).values():
                if order.get('credited') and order.get('trans_id'):
                    self.db.execute('INSERT OR IGNORE INTO payment_claims VALUES (?,?)', ('crypto', str(order['trans_id'])))
            self.db.execute("INSERT INTO metadata VALUES ('integer_vnd_v1', '1')")

    def payments_paused(self):
        row = self.db.execute("SELECT value FROM metadata WHERE key='payments_paused_after_restore'").fetchone()
        return row is not None and row[0] == '1'

    def claim_crypto_payment(self, payment_id):
        return self.db.execute('INSERT OR IGNORE INTO payment_claims VALUES (?,?)', ('crypto', str(payment_id))).rowcount == 1

    def observe_bank_payment(self, transaction_id, amount, description, reason):
        from money import vnd
        self.db.execute('INSERT OR IGNORE INTO bank_payments VALUES (?,?,?,?,?,?,?,?)',
            (str(transaction_id), vnd(amount), str(description)[:1000], 'pending', reason, None, None, datetime.now().isoformat()))
        return self.bank_payment(transaction_id)

    def bank_payment(self, transaction_id):
        self.db.row_factory = sqlite3.Row
        try:
            row = self.db.execute('SELECT * FROM bank_payments WHERE transaction_id=?', (str(transaction_id),)).fetchone()
            return dict(row) if row else None
        finally:
            self.db.row_factory = None

    def pending_bank_payments(self, limit=10):
        rows = self.db.execute("SELECT transaction_id FROM bank_payments WHERE status='pending' ORDER BY created_at LIMIT ?", (limit,)).fetchall()
        return [self.bank_payment(row[0]) for row in rows]

    def settle_bank_payment(self, transaction_id, status, user_id, actor_id):
        changed = self.db.execute("UPDATE bank_payments SET status=?, user_id=?, actor_id=? WHERE transaction_id=? AND status='pending'",
                                 (status, str(user_id) if user_id is not None else None, str(actor_id), str(transaction_id))).rowcount
        if changed != 1:
            raise ValueError('Payment is no longer pending')

    def enqueue_delivery(self, order_id, user_id):
        self.db.execute("INSERT OR IGNORE INTO deliveries(order_id,user_id,status,attempts,next_attempt_at) VALUES (?,?,'pending',0,0)", (order_id, str(user_id)))

    def delivery(self, order_id):
        row = self.db.execute('SELECT order_id,user_id,status,attempts,next_attempt_at,last_error,message_id FROM deliveries WHERE order_id=?', (order_id,)).fetchone()
        return dict(zip(('order_id','user_id','status','attempts','next_attempt_at','last_error','message_id'), row)) if row else None

    def delivery_pending(self, order_id):
        row = self.delivery(order_id)
        return row is not None and row['status'] != 'sent'

    def due_deliveries(self, now):
        return [row[0] for row in self.db.execute("SELECT order_id FROM deliveries WHERE status IN ('pending','failed','sending') AND next_attempt_at<=? AND attempts<5 ORDER BY next_attempt_at LIMIT 10", (now,))]

    def import_stock(self, source, product_id='default'):
        lines = [line.strip() for line in Path(source).read_text(encoding='utf-8-sig').splitlines() if line.strip()]
        if not lines:
            raise ValueError('The account file is empty')
        if len(set(lines)) != len(lines):
            raise ValueError('The import file contains duplicate accounts')
        with self.transaction():
            path = self.root / 'data/accounts.txt'
            catalog = self.load_json(self.root / 'data/products.json', {'products': []})
            if product_id == 'default' and catalog.get('default_deleted') is True:
                raise ValueError('The default product has been removed')
            if product_id != 'default':
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
        from operations import snapshot_database
        target = Path(target).resolve()
        if target.exists() or target == self.path:
            raise ValueError('Choose a new backup filename')
        if self.db.in_transaction:
            raise ValueError('Back up only committed transactions')
        return snapshot_database(self.path, target)

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
        try:
            lock = acquire_process_lock(args.root / 'data')
        except OSError:
            parser.error('Cannot acquire data lock; stop the bot and check directory permissions')
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
