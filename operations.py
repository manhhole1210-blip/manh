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
