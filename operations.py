"""Local runtime policy, consistent SQLite backups and verified restore."""
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ADMINS = [920604650602909767, 909652269723693077]
DEFAULTS = {
    'allowed_guild_ids': [], 'admin_user_ids': DEFAULT_ADMINS,
    'alert_channel_id': 0, 'bootstrap_from_stock_channel': True,
    'backup_interval_hours': 6, 'backup_keep': 28,
    'bank_failure_threshold': 3, 'alert_repeat_seconds': 3600,
    'bank_poll_seconds': 30, 'crypto_poll_seconds': 30,
}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class RuntimePolicy:
    def __init__(self, root):
        self.path = Path(root) / 'operations.json'
        values = json.loads(self.path.read_text(encoding='utf-8-sig')) if self.path.exists() else {}
        if not isinstance(values, dict):
            raise ValueError('operations.json must contain an object')
        self.config = {**DEFAULTS, **values}
        for key in ('allowed_guild_ids', 'admin_user_ids'):
            if not isinstance(self.config[key], list):
                raise ValueError(f'{key} must be a list of Discord IDs')
            self.config[key] = [self._id(value) for value in self.config[key]]
        self.config['alert_channel_id'] = self._id(self.config['alert_channel_id'], allow_zero=True)
        for key, minimum, maximum in (
            ('backup_interval_hours', 1, 168), ('backup_keep', 1, 365),
            ('bank_failure_threshold', 1, 100), ('alert_repeat_seconds', 60, 86400),
            ('bank_poll_seconds', 5, 3600), ('crypto_poll_seconds', 5, 3600),
        ):
            value = self.config[key]
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f'Invalid {key} in operations.json')
        if not isinstance(self.config['bootstrap_from_stock_channel'], bool):
            raise ValueError('bootstrap_from_stock_channel must be boolean')
        if not self.path.exists():
            self.save()

    @staticmethod
    def _id(value, allow_zero=False):
        if isinstance(value, bool) or not str(value).isdigit():
            raise ValueError('Discord IDs must be positive integers')
        result = int(value)
        if result < (0 if allow_zero else 1):
            raise ValueError('Invalid Discord ID')
        return result

    def save(self):
        atomic_json(self.path, self.config)

    def allowed_guild(self, guild_id):
        return guild_id is not None and int(guild_id) in self.config['allowed_guild_ids']

    def can_manage(self, interaction):
        guild = getattr(interaction, 'guild', None)
        permissions = getattr(interaction, 'permissions', None)
        return (guild is not None and self.allowed_guild(guild.id)
                and interaction.user.id in self.config['admin_user_ids']
                and bool(getattr(permissions, 'administrator', False)))


def utc_stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def readonly_database(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=30)


def validate_database(connection):
    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('SQLite integrity check failed')
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'users', 'orders', 'documents', 'stock', 'metadata', 'processed_transactions'} <= tables:
        raise ValueError('Not a compatible bot database')
    if not connection.execute("SELECT 1 FROM metadata WHERE key='legacy_import_v1'").fetchone():
        raise ValueError('Database migration was not completed')


def snapshot_database(source, target):
    """Open an independent read connection: safe in a worker thread with WAL writers."""
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise ValueError('Backup target must differ from source')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        origin = readonly_database(source)
        try:
            destination = sqlite3.connect(target)
            try:
                origin.backup(destination, pages=256)
                if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup integrity check failed')
            finally:
                destination.close()
        finally:
            origin.close()
        with target.open('rb+') as handle:
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def rotating_backup(root, keep=28):
    root = Path(root)
    folder = root / 'backups/sqlite'
    target = snapshot_database(root / 'data/bot.db', folder / f'bot-{utc_stamp()}.db')
    # Only prune files this function owns, after the new backup passes integrity checks.
    candidates = sorted(p for p in folder.glob('bot-*.db') if re.fullmatch(r'bot-\d{8}T\d{12}Z\.db', p.name))
    for path in candidates[:-keep]:
        path.unlink()
    return target


def restore_database(root, backup):
    """Offline restore; hold the same process lock used by the bot and maintenance."""
    from sqlite_store import acquire_process_lock
    root, backup = Path(root).resolve(), Path(backup).resolve()
    database = root / 'data/bot.db'
    if backup == database:
        raise ValueError('Choose a separate backup file')
    lock = acquire_process_lock(root / 'data')
    staging = root / 'data' / f'.restore-{utc_stamp()}.db'
    try:
        source = readonly_database(backup)
        try:
            validate_database(source)
        finally:
            source.close()
        snapshot_database(backup, staging)
        staged = sqlite3.connect(staging)
        try:
            staged.execute("INSERT INTO metadata VALUES ('payments_paused_after_restore','1') ON CONFLICT(key) DO UPDATE SET value='1'")
            staged.commit()
        finally:
            staged.close()
        # Preserve current committed data before replacing it, including WAL contents.
        preserved = None
        if database.exists():
            preserved = snapshot_database(database, root / 'backups' / f'pre-restore-{utc_stamp()}.db')
            current = sqlite3.connect(database, timeout=30)
            try:
                if current.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                    raise RuntimeError('Database is still in use; restore aborted')
            finally:
                current.close()
        for suffix in ('-wal', '-shm'):
            Path(str(database) + suffix).unlink(missing_ok=True)
        os.replace(staging, database)
        if os.name == 'posix':
            fd = os.open(database.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return preserved
    finally:
        staging.unlink(missing_ok=True)
        lock.close()


def parse_rounding_plan(text):
    """Require explicit ID=old-balance pairs; never round every wallet implicitly."""
    from decimal import Decimal, InvalidOperation
    from money import MAX_VND
    plan = {}
    for entry in text.split(','):
        user_id, separator, raw = entry.strip().partition('=')
        if not separator or not user_id.isdigit() or len(user_id) > 22 or user_id in plan:
            raise ValueError('Use unique Discord_ID=approved_old_balance pairs separated by commas')
        if len(raw) > 100:
            raise ValueError('Invalid approved balance')
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            raise ValueError('Invalid approved balance') from None
        if not amount.is_finite() or amount < 0 or amount > MAX_VND or amount == amount.to_integral_value():
            raise ValueError('Each approved old balance must be finite, nonnegative and fractional')
        plan[user_id] = amount
    if not plan:
        raise ValueError('Rounding plan is empty')
    return plan


def round_approved_balances(root, plan):
    """Offline, backup-first, exact-match, atomic wallet rounding with durable audit."""
    from decimal import Decimal, ROUND_HALF_UP
    import hashlib
    from money import vnd
    from sqlite_store import acquire_process_lock
    root = Path(root).resolve()
    database = root / 'data/bot.db'
    # Validate programmatic callers as strictly as CLI input.
    plan = parse_rounding_plan(','.join(f'{uid}={value}' for uid, value in sorted(plan.items())))
    changes = [dict(user_id=uid, old=str(amount), new=vnd(amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP)),
                    delta=str(amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP) - amount))
               for uid, amount in sorted(plan.items())]
    key = 'approved_balance_rounding:' + hashlib.sha256(json.dumps(changes, sort_keys=True).encode()).hexdigest()
    lock = acquire_process_lock(root / 'data')
    connection = None
    try:
        def validate_wallets(db):
            for item in changes:
                row = db.execute('SELECT payload FROM users WHERE id=?', (item['user_id'],)).fetchone()
                if row is None:
                    raise ValueError(f"User {item['user_id']} does not exist; no balances changed")
                user = json.loads(row[0])
                balance = user.get('balance') if isinstance(user, dict) else None
                try:
                    matches = not isinstance(balance, bool) and Decimal(str(balance)) == plan[item['user_id']]
                except Exception:
                    matches = False
                if not matches:
                    raise ValueError(f"Balance changed for user {item['user_id']}; expected plan no longer matches. No balances changed")
        source = readonly_database(database)
        try:
            validate_database(source)
            previous = source.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            if previous:
                return {**json.loads(previous[0]), 'already_applied': True}
            validate_wallets(source)
        finally:
            source.close()
        backup = snapshot_database(database, root / 'backups' / f'pre-round-vnd-{utc_stamp()}.db')
        connection = sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=30)
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('BEGIN IMMEDIATE')
        validate_wallets(connection)  # Catch external changes between preflight and the write lock.
        marker = connection.execute("SELECT 1 FROM documents WHERE path='data/users.json'").fetchone()
        if marker is None:
            raise ValueError('Missing users collection marker; no balances changed')
        row = connection.execute("SELECT payload FROM documents WHERE path='data/activity_logs.json'").fetchone()
        logs = json.loads(row[0]) if row else {}
        if not isinstance(logs, dict):
            raise ValueError('Invalid audit history structure; no balances changed')
        created_at = datetime.now(timezone.utc).isoformat()
        manifest = {'created_at': created_at, 'rule': 'ROUND_HALF_UP', 'actor': 'owner-approved-local-cli',
                    'backup': str(backup.relative_to(root)), 'changes': changes}
        for item in changes:
            user_id = item['user_id']
            user = json.loads(connection.execute('SELECT payload FROM users WHERE id=?', (user_id,)).fetchone()[0])
            user['balance'] = item['new']
            connection.execute('UPDATE users SET payload=? WHERE id=?',
                               (json.dumps(user, ensure_ascii=False, allow_nan=False), user_id))
            bucket = logs.setdefault(user_id, [])
            if not isinstance(bucket, list):
                raise ValueError(f'Invalid audit history for {user_id}; no balances changed')
            bucket.append({'timestamp': created_at, 'user_id': int(user_id), 'username': str(user.get('username', '')),
                'action': 'balance_rounding_adjustment', 'interaction_type': 'local_cli',
                'details': {'balance_before': item['old'], 'balance_after': item['new'], 'delta_vnd': item['delta'],
                            'rounding_rule': 'ROUND_HALF_UP', 'approval_id': key, 'backup': manifest['backup']}})
        connection.execute('INSERT INTO documents VALUES (?,?) ON CONFLICT(path) DO UPDATE SET payload=excluded.payload',
                           ('data/activity_logs.json', json.dumps(logs, ensure_ascii=False, allow_nan=False)))
        connection.execute('INSERT INTO metadata VALUES (?,?)', (key, json.dumps(manifest, allow_nan=False)))
        connection.execute('COMMIT')
        return {**manifest, 'already_applied': False}
    except BaseException:
        if connection is not None and connection.in_transaction:
            connection.execute('ROLLBACK')
        raise
    finally:
        if connection is not None:
            connection.close()
        lock.close()
