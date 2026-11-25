"""Vendor foundation tests use only an in-memory database and synthetic keys."""

import datetime as dt
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from laptopseal_vendor import (
    VendorLicenseError, approve_challenge, challenge_status, create_challenge,
    ensure_vendor_indexes, issue_test_package, signed_entitlement,
    upsert_verified_vendor, verify_entitlement,
)


NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=1)
DEVICE1 = 'synthetic-device-00000001'
DEVICE2 = 'synthetic-device-00000002'


class VendorFoundationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'LAPTOPSEAL_TEST_MODE': '1',
                                         'MONGO_DB_NAME': 'laptopseal_test'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db = mongomock.MongoClient()['laptopseal_test']
        ensure_vendor_indexes(self.db)
        self.vendor = upsert_verified_vendor(self.db, 'vendor@example.test', NOW)
        self.other = upsert_verified_vendor(self.db, 'other@example.test', NOW)
        self.lock = threading.RLock()

    def transaction(self, callback):
        # mongomock has no MongoDB sessions. Serialize this in-memory fixture;
        # production always uses a MongoDB transaction.
        with self.lock:
            return callback(None)

    def package(self, plan='single', now=NOW):
        return issue_test_package(self.db, self.vendor['_id'], plan, now=now)

    def approve(self, package, device=DEVICE1, at=NOW):
        challenge = create_challenge(self.db, device, now=at)
        allocation = approve_challenge(
            self.db, challenge['challenge_id'], challenge['qr_secret'],
            self.vendor['_id'], package['_id'], now=at,
            transaction_runner=self.transaction)
        return challenge, allocation

    def test_multiple_packages_and_independent_expiry(self):
        a = self.package(now=NOW)
        b = self.package(now=NOW + dt.timedelta(days=5))
        self.assertNotEqual(a['_id'], b['_id'])
        self.assertEqual((b['expires_at'] - a['expires_at']).days, 5)
        self.assertEqual((a['expires_at'] - a['issued_at']).days, 30)

    def test_expired_device_can_use_new_package_without_reclaiming_old_slot(self):
        old = self.package()
        self.approve(old)
        new = self.package(now=NOW + dt.timedelta(days=31))
        self.approve(new, DEVICE1, NOW + dt.timedelta(days=31))
        self.assertEqual(self.db.ls_vendor_allocations.count_documents({}), 2)
        self.assertEqual(self.db.ls_vendor_packages.find_one({'_id': old['_id']})['allocated_count'], 1)

    def test_amounts_capacity_and_test_guard(self):
        for plan, amount, capacity in [('single', 200000, 1), ('basic', 800000, 5),
                                       ('business', 3000000, 20), ('unlimited', 6000000, 200)]:
            package = self.package(plan)
            self.assertEqual((package['amount_kobo'], package['max_devices']),
                             (amount, capacity))
        with patch.dict(os.environ, {'LAPTOPSEAL_TEST_MODE': '0'}):
            with self.assertRaises(VendorLicenseError):
                self.package()
        self.assertEqual(self.db.ls_vendor_packages.count_documents({}), 4)

    def test_new_device_consumes_slot_and_repeated_device_does_not(self):
        package = self.package()
        _, first = self.approve(package)
        _, again = self.approve(package)
        self.assertEqual(first['_id'], again['_id'])
        self.assertEqual(self.db.ls_vendor_packages.find_one({'_id': package['_id']})['allocated_count'], 1)

    def test_different_devices_and_capacity(self):
        package = self.package('basic')
        for i in range(5):
            self.approve(package, f'synthetic-device-{i:08d}')
        self.assertEqual(self.db.ls_vendor_allocations.count_documents({'package_id': package['_id']}), 5)
        with self.assertRaisesRegex(VendorLicenseError, 'limit'):
            self.approve(package, 'synthetic-device-over-limit')

    def test_concurrent_approvals_do_not_oversubscribe(self):
        package = self.package()
        challenges = [create_challenge(self.db, d, now=NOW) for d in (DEVICE1, DEVICE2)]
        results = []

        def worker(challenge):
            try:
                approve_challenge(self.db, challenge['challenge_id'], challenge['qr_secret'],
                                  self.vendor['_id'], package['_id'], now=NOW,
                                  transaction_runner=self.transaction)
                results.append('approved')
            except VendorLicenseError:
                results.append('rejected')

        threads = [threading.Thread(target=worker, args=(challenge,)) for challenge in challenges]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(results, ['approved', 'rejected'])
        self.assertEqual(self.db.ls_vendor_packages.find_one({'_id': package['_id']})['allocated_count'], 1)

    def test_expired_package_rejects_new_allocation_and_old_slots_expire(self):
        package = self.package()
        self.approve(package)
        with self.assertRaises(VendorLicenseError):
            self.approve(package, DEVICE2, NOW + dt.timedelta(days=30))
        key = Ed25519PrivateKey.generate()
        challenge = create_challenge(self.db, DEVICE1, now=NOW)
        approve_challenge(self.db, challenge['challenge_id'], challenge['qr_secret'],
                          self.vendor['_id'], package['_id'], now=NOW,
                          transaction_runner=self.transaction)
        with self.assertRaises(VendorLicenseError):
            signed_entitlement(self.db, challenge['challenge_id'], challenge['poll_secret'],
                               key, now=NOW + dt.timedelta(days=30))

    def test_expired_challenge_and_replay(self):
        package = self.package()
        challenge = create_challenge(self.db, DEVICE1, now=NOW)
        with self.assertRaises(VendorLicenseError):
            approve_challenge(self.db, challenge['challenge_id'], challenge['qr_secret'],
                              self.vendor['_id'], package['_id'],
                              now=NOW + dt.timedelta(minutes=5),
                              transaction_runner=self.transaction)
        self.approve(package)
        fresh = create_challenge(self.db, DEVICE1, now=NOW)
        approve_challenge(self.db, fresh['challenge_id'], fresh['qr_secret'],
                          self.vendor['_id'], package['_id'], now=NOW,
                          transaction_runner=self.transaction)
        with self.assertRaises(VendorLicenseError):
            approve_challenge(self.db, fresh['challenge_id'], fresh['qr_secret'],
                              self.vendor['_id'], package['_id'], now=NOW,
                              transaction_runner=self.transaction)

    def test_cross_account_package_is_rejected(self):
        package = self.package()
        challenge = create_challenge(self.db, DEVICE1, now=NOW)
        with self.assertRaises(VendorLicenseError):
            approve_challenge(self.db, challenge['challenge_id'], challenge['qr_secret'],
                              self.other['_id'], package['_id'], now=NOW,
                              transaction_runner=self.transaction)
        self.assertEqual(self.db.ls_vendor_allocations.count_documents({}), 0)

    def test_invalid_device_and_poll_secret_are_rejected(self):
        with self.assertRaises(VendorLicenseError):
            create_challenge(self.db, 'a', now=NOW)
        challenge = create_challenge(self.db, DEVICE1, now=NOW)
        with self.assertRaises(VendorLicenseError):
            challenge_status(self.db, challenge['challenge_id'], 'wrong')
        self.assertEqual(challenge_status(self.db, challenge['challenge_id'],
                                          challenge['poll_secret'], now=NOW)['status'], 'pending')

    def test_signed_proof_device_expiry_environment_and_tampering(self):
        package = self.package()
        challenge, _ = self.approve(package)
        key = Ed25519PrivateKey.generate()
        proof = signed_entitlement(self.db, challenge['challenge_id'],
                                   challenge['poll_secret'], key, now=NOW)
        public = key.public_key()
        self.assertTrue(verify_entitlement(proof, public, DEVICE1, 'test', now=NOW))
        self.assertFalse(verify_entitlement(proof, public, DEVICE2, 'test', now=NOW))
        self.assertFalse(verify_entitlement(proof, public, DEVICE1, 'production', now=NOW))
        self.assertFalse(verify_entitlement(proof, public, DEVICE1, 'test',
                                            now=NOW + dt.timedelta(days=30)))
        self.assertFalse(verify_entitlement(proof, public, DEVICE1, 'test', now=NOW,
                                            last_trusted_at=NOW + dt.timedelta(minutes=1)))
        forged = {'payload': dict(proof['payload']), 'signature': proof['signature']}
        forged['payload']['expires_at'] = '2099-01-01T00:00:00Z'
        self.assertFalse(verify_entitlement(forged, public, DEVICE1, 'test', now=NOW))

    def test_test_package_cannot_be_approved_outside_test_mode(self):
        package = self.package()
        challenge = create_challenge(self.db, DEVICE1, now=NOW)
        with patch.dict(os.environ, {'LAPTOPSEAL_TEST_MODE': '0'}):
            with self.assertRaises(VendorLicenseError):
                approve_challenge(self.db, challenge['challenge_id'], challenge['qr_secret'],
                                  self.vendor['_id'], package['_id'], now=NOW,
                                  transaction_runner=self.transaction)

    def test_unverified_vendor_and_production_db_cannot_issue_test_package(self):
        self.db.ls_vendor_identities.insert_one({'_id': 'unverified', 'email': 'u@example.test'})
        with self.assertRaises(VendorLicenseError):
            issue_test_package(self.db, 'unverified', 'single', now=NOW)
        other_db = mongomock.MongoClient()['lightideas']
        with self.assertRaises(VendorLicenseError):
            issue_test_package(other_db, self.vendor['_id'], 'single', now=NOW)


if __name__ == '__main__':
    unittest.main()
