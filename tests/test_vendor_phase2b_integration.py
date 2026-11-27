"""Cross-project synthetic QR activation, with no real DB, mail, or payment."""

import datetime as dt
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import mongomock
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask import Flask

from laptopseal_vendor import ensure_vendor_indexes, issue_test_package
from laptopseal_vendor_api import PREFIX, register_vendor_api
from laptopseal_vendor_auth import ensure_vendor_auth_indexes


DEVICE = 'c' * 32
DESKTOP_MODULE = Path.home() / 'Desktop' / 'LightIdeas_Diagnostic' / 'vendor_entitlement.py'


class VendorPhase2BIntegration(unittest.TestCase):
    def test_email_to_signed_desktop_access(self):
        if not DESKTOP_MODULE.is_file():
            self.skipTest('Desktop source checkout is unavailable on this machine.')
        spec = importlib.util.spec_from_file_location('desktop_vendor_entitlement', DESKTOP_MODULE)
        desktop = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(desktop)
        with patch.dict(os.environ, {'LAPTOPSEAL_TEST_MODE': '1',
                                     'MONGO_DB_NAME': 'laptopseal_test',
                                     'LAPTOPSEAL_LICENSE_PEPPER': 'synthetic-phase2b-pepper'}):
            db = mongomock.MongoClient()['laptopseal_test']
            ensure_vendor_indexes(db)
            ensure_vendor_auth_indexes(db)
            mail = []
            key = Ed25519PrivateKey.generate()
            lock = threading.RLock()
            app = Flask(__name__)
            app.config['TESTING'] = True

            def transaction(callback):
                with lock:
                    return callback(None)

            register_vendor_api(app, lambda: db, lambda *args: False,
                                deliver_code=lambda email, code: mail.append((email, code)) or True,
                                signing_key_loader=lambda: key,
                                transaction_runner=transaction)
            laptop = app.test_client()
            phone = app.test_client()
            challenge = laptop.post(PREFIX + '/challenges', json={'hardware_id': DEVICE})
            self.assertEqual(challenge.status_code, 201)
            qr = challenge.json['qr']
            self.assertNotIn('poll_secret', qr)
            pending = laptop.post(PREFIX + '/challenges/' + qr['challenge_id'] + '/poll',
                                  json={'poll_secret': challenge.json['poll_secret']})
            self.assertEqual(pending.json['status'], 'pending')

            address = 'synthetic-vendor@example.test'
            self.assertEqual(phone.post(PREFIX + '/auth/request',
                                        json={'email': address}).status_code, 202)
            code = mail[-1][1]
            verified = phone.post(PREFIX + '/auth/verify',
                                  json={'email': address, 'code': code})
            self.assertEqual(verified.status_code, 200)
            csrf = verified.json['csrf_token']
            vendor = db.ls_vendor_identities.find_one({'email': address})
            package = issue_test_package(db, vendor['_id'], 'single')
            listing = phone.get(PREFIX + '/packages')
            self.assertEqual(listing.json['packages'][0]['remaining_slots'], 1)
            inspect = phone.post(PREFIX + '/challenges/' + qr['challenge_id'] + '/inspect',
                                 json={'qr_secret': qr['qr_secret']},
                                 headers={'X-LaptopSeal-CSRF': csrf})
            self.assertEqual(inspect.status_code, 200)
            approved = phone.post(PREFIX + '/challenges/' + qr['challenge_id'] + '/approve',
                                  json={'qr_secret': qr['qr_secret'], 'package_id': package['_id']},
                                  headers={'X-LaptopSeal-CSRF': csrf})
            self.assertEqual(approved.status_code, 200)
            self.assertNotIn('entitlement', approved.json)
            received = laptop.post(PREFIX + '/challenges/' + qr['challenge_id'] + '/poll',
                                   json={'poll_secret': challenge.json['poll_secret']})
            self.assertEqual(received.status_code, 200)
            proof = received.json['entitlement']
            self.assertIsNotNone(desktop.verify_proof(proof, key.public_key(), DEVICE, 'test'))
            self.assertIsNone(desktop.verify_proof(proof, key.public_key(), 'd' * 32, 'test'))
            with tempfile.TemporaryDirectory() as temp:
                path = str(Path(temp) / 'vendor_entitlement.json')
                self.assertTrue(desktop.save_state(path, proof))
                self.assertIsNotNone(desktop.offline_access(path, key.public_key(), DEVICE, 'test'))
            renewed = laptop.post(PREFIX + '/entitlements/check',
                                  json={'entitlement': proof, 'hardware_id': DEVICE})
            self.assertEqual(renewed.status_code, 200)
            self.assertEqual(db.ls_vendor_allocations.count_documents({}), 1)
            self.assertEqual(db.ls_vendor_packages.find_one({'_id': package['_id']})['allocated_count'], 1)


if __name__ == '__main__':
    unittest.main()
