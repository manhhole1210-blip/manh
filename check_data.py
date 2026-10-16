"""Read-only JSON diagnostic. No token values or file contents are printed."""
import argparse
from pathlib import Path

from sqlite_store import read_legacy_json


def main():
    parser = argparse.ArgumentParser(description='Check legacy bot JSON without changing data')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    root = args.root
    paths = set((root / 'data').glob('*.json'))
    paths.update((root / 'data/orders').glob('*.json'))
    paths.update(root / name for name in ('config.json', 'tickets.json', 'user_languages.json', 'autocrypto/config.json'))
    errors = 0
    checked = 0
    for path in sorted(paths):
        if not path.is_file():
            continue
        checked += 1
        try:
            read_legacy_json(path)
        except (ValueError, OSError) as exc:
            errors += 1
            print(f'ERROR: {exc}')
        else:
            print(f'OK: {path}')
    print(f'Checked {checked} files; {errors} errors. No files were changed.')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
