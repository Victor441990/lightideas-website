"""Create or reuse one synthetic vendor Single package in laptopseal_test."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEST_EMAIL = 'vendor.laptopseal.test@example.test'


def main():
    if (os.environ.get('LAPTOPSEAL_TEST_MODE') != '1'
            or os.environ.get('MONGO_DB_NAME') != 'laptopseal_test'
            or not os.environ.get('MONGO_URI')):
        raise SystemExit('Refusing test package setup outside laptopseal_test.')
    from app import get_db
    from laptopseal_vendor import issue_test_package, upsert_verified_vendor, utcnow

    db = get_db()
    if db.name != 'laptopseal_test':
        raise SystemExit('Selected database is not laptopseal_test.')
    db.command('ping')
    vendor = upsert_verified_vendor(db, TEST_EMAIL, utcnow())
    existing = db.ls_vendor_packages.find_one({
        'vendor_id': vendor['_id'], 'plan_id': 'single',
        'source': 'isolated_test_only', 'status': 'active',
        'expires_at': {'$gt': utcnow().replace(tzinfo=None)}})
    if existing:
        print('Synthetic vendor test package already available in laptopseal_test.')
        return
    issue_test_package(db, vendor['_id'], 'single')
    print('Synthetic vendor Single package created in laptopseal_test.')


if __name__ == '__main__':
    main()
