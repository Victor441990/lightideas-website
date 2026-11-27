"""Explicit, additive vendor index setup for laptopseal_test only."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    if (os.environ.get('LAPTOPSEAL_TEST_MODE') != '1'
            or os.environ.get('MONGO_DB_NAME') != 'laptopseal_test'
            or not os.environ.get('MONGO_URI')):
        raise SystemExit('Refusing migration: isolated laptopseal_test configuration required.')
    from app import get_db
    from laptopseal_vendor import ensure_vendor_indexes
    from laptopseal_vendor_auth import ensure_vendor_auth_indexes

    db = get_db()
    if db.name != 'laptopseal_test':
        raise SystemExit('Refusing migration: selected database is not laptopseal_test.')
    db.command('ping')
    ensure_vendor_indexes(db)
    ensure_vendor_auth_indexes(db)
    print('Vendor indexes ready in laptopseal_test.')


if __name__ == '__main__':
    main()
