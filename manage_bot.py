"""Local configuration and offline recovery. Never accepts secret values."""
import argparse
from pathlib import Path
from operations import RuntimePolicy, restore_database, rotating_backup


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('configure')
    init.add_argument('--guild-id', required=True)
    init.add_argument('--admin-id', action='append', required=True)
    init.add_argument('--alert-channel-id', default='0')
    sub.add_parser('backup')
    resume = sub.add_parser('resume-payments')
    resume.add_argument('--confirm-reconciled', action='store_true', required=True)
    restore = sub.add_parser('restore')
    restore.add_argument('file', type=Path)
    args = parser.parse_args()
    if args.command == 'configure':
        from sqlite_store import acquire_process_lock
        lock = acquire_process_lock(args.root / 'data')
        try:
            policy = RuntimePolicy(args.root)
            policy.config.update(allowed_guild_ids=[policy._id(args.guild_id)],
                admin_user_ids=[policy._id(value) for value in args.admin_id],
                alert_channel_id=policy._id(args.alert_channel_id, allow_zero=True), bootstrap_from_stock_channel=False)
            policy.save()
            print('Configuration saved. Restart the bot to apply.')
        finally:
            lock.close()
    elif args.command == 'backup':
        policy = RuntimePolicy(args.root)
        print('Verified backup:', rotating_backup(args.root, policy.config['backup_keep']))
    elif args.command == 'resume-payments':
        from sqlite_store import acquire_process_lock
        import sqlite3
        lock = acquire_process_lock(args.root / 'data')
        try:
            path = args.root / 'data/bot.db'
            if not path.is_file():
                raise ValueError('Database does not exist')
            connection = sqlite3.connect(path)
            try:
                connection.execute("UPDATE metadata SET value='0' WHERE key='payments_paused_after_restore'")
                connection.commit()
            finally:
                connection.close()
            print('Automatic payments resumed. Existing pending transfers still require manual reconciliation.')
        finally:
            lock.close()
    else:
        preserved = restore_database(args.root, args.file)
        print('Restore completed. Previous database preserved at:', preserved)
        print('Keep the bot stopped until the restored stock and balances are reconciled with payments received after that backup.')


if __name__ == '__main__':
    main()
