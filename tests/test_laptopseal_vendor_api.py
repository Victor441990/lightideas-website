"""Isolated vendor HTTP tests: in-memory MongoDB, synthetic mail and keys."""

import datetime as dt
import os
import re
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import mongomock
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from laptopseal_vendor import ensure_vendor_indexes, issue_test_package, verify_entitlement
from laptopseal_vendor_api import PREFIX, register_vendor_api
from laptopseal_vendor_auth import ensure_vendor_auth_indexes


DEVICE1 = 'a' * 32
DEVICE2 = 'b' * 32


class VendorApiTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'LAPTOPSEAL_TEST_MODE': '1', 'MONGO_DB_NAME': 'laptopseal_test',
            'LAPTOPSEAL_LICENSE_PEPPER': 'synthetic-vendor-test-pepper-only',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db = mongomock.MongoClient()['laptopseal_test']
        ensure_vendor_indexes(self.db)
        ensure_vendor_auth_indexes(self.db)
        self.mail = []
        self.key = Ed25519PrivateKey.generate()
        self.lock = threading.RLock()
        app = Flask(__name__)
        app.config['TESTING'] = True

        def transaction(callback):
            with self.lock:
                return callback(None)

        register_vendor_api(app, lambda: self.db, lambda *args: False,
                            deliver_code=lambda email, code: self.mail.append((email, code)) or True,
                            signing_key_loader=lambda: self.key,
                            transaction_runner=transaction)
        self.client = app.test_client()

    def test_index_setup_is_additive_and_idempotent(self):
        before = {name: sorted(self.db[name].index_information()) for name in
                  ('ls_vendor_identities', 'ls_vendor_packages',
                   'ls_vendor_allocations', 'ls_vendor_challenges',
                   'ls_vendor_email_codes', 'ls_vendor_auth_counters',
                   'ls_vendor_sessions')}
        ensure_vendor_indexes(self.db)
        ensure_vendor_auth_indexes(self.db)
        after = {name: sorted(self.db[name].index_information()) for name in before}
        self.assertEqual(before, after)

    def test_local_test_capture_never_sends_real_email(self):
        app = Flask(__name__)
        app.config['TESTING'] = True
        real_sender = Mock(return_value=True)
        register_vendor_api(app, lambda: self.db, real_sender)
        client = app.test_client()
        with patch.dict(os.environ, {'LAPTOPSEAL_VENDOR_TEST_CODE_CAPTURE': '1'}), \
             patch('laptopseal_vendor_api.Path.write_text') as write_code, \
             patch('laptopseal_vendor_api.os.chmod'):
            result = client.post(PREFIX + '/auth/request',
                                 json={'email': 'vendor@example.test'})
        self.assertEqual(result.status_code, 202)
        self.assertTrue(write_code.called)
        real_sender.assert_not_called()

    def test_phone_test_email_is_disabled_until_explicitly_enabled(self):
        app = Flask(__name__)
        app.config['TESTING'] = True
        sender = Mock(return_value=True)
        register_vendor_api(app, lambda: self.db, sender)
        host = 'owner-test.trycloudflare.com'
        with patch.dict(os.environ, {
                'LAPTOPSEAL_PHONE_TEST_HOST': host,
                'LAPTOPSEAL_PHONE_TEST_EMAIL': 'owner@example.test',
                'LAPTOPSEAL_PHONE_TEST_EMAIL_ENABLED': '0',
                'LAPTOPSEAL_PHONE_TEST_GATEWAY_TOKEN': 'g' * 43,
                'BREVO_API_KEY': 'synthetic-only'}):
            response = app.test_client().post(
                PREFIX + '/auth/request', json={'email': 'owner@example.test'},
                base_url='https://' + host,
                headers={'Origin': 'https://' + host,
                         'X-LaptopSeal-Phone-Gateway': 'g' * 43})
        self.assertEqual(response.status_code, 202)
        sender.assert_not_called()

    def test_phone_test_email_requires_exact_host_email_and_gateway_token(self):
        app = Flask(__name__)
        app.config['TESTING'] = True
        sender = Mock(return_value=True)
        register_vendor_api(app, lambda: self.db, sender)
        host = 'desktop-test.tail1234.ts.net'
        client = app.test_client()
        with patch.dict(os.environ, {
                'LAPTOPSEAL_PHONE_TEST_HOST': host,
                'LAPTOPSEAL_PHONE_TEST_EMAIL': 'owner@example.test',
                'LAPTOPSEAL_PHONE_TEST_EMAIL_ENABLED': '1',
                'LAPTOPSEAL_PHONE_TEST_GATEWAY_TOKEN': 'g' * 43,
                'BREVO_API_KEY': 'synthetic-only'}):
            missing_token = client.post(
                PREFIX + '/auth/request', json={'email': 'owner@example.test'},
                base_url='https://' + host, headers={'Origin': 'https://' + host})
            self.assertEqual(missing_token.status_code, 202)
            sender.assert_not_called()
            self.db.ls_vendor_auth_counters.delete_many({})
            allowed = client.post(
                PREFIX + '/auth/request', json={'email': 'owner@example.test'},
                base_url='https://' + host,
                headers={'Origin': 'https://' + host,
                         'X-LaptopSeal-Phone-Gateway': 'g' * 43})
            self.assertEqual(allowed.status_code, 202)
            sender.assert_called_once()
            self.assertNotIn('Your LaptopSeal sign-in code is', allowed.get_data(as_text=True))
            code = re.search(r'\b\d{8}\b', sender.call_args.args[3]).group()
            verified = client.post(
                PREFIX + '/auth/verify', json={'email': 'owner@example.test', 'code': code},
                base_url='https://' + host,
                headers={'Origin': 'https://' + host})
            self.assertEqual(verified.status_code, 200)
            self.assertIn('Secure', verified.headers['Set-Cookie'])

    def post(self, path, payload, client=None, csrf=None):
        headers = {'X-LaptopSeal-CSRF': csrf} if csrf else {}
        return (client or self.client).post(PREFIX + path, json=payload, headers=headers)

    def login(self, email='vendor@example.test', client=None):
        client = client or self.client
        result = self.post('/auth/request', {'email': email}, client)
        self.assertEqual(result.status_code, 202)
        code = self.mail[-1][1]
        result = self.post('/auth/verify', {'email': email, 'code': code}, client)
        self.assertEqual(result.status_code, 200)
        self.assertNotIn(code, str(result.json))
        return result.json['csrf_token']

    def test_valid_email_session_cookie_and_rotation(self):
        csrf = self.login()
        self.assertEqual(len(csrf) >= 32, True)
        status = self.client.get(PREFIX + '/session')
        self.assertEqual(status.status_code, 200)
        self.assertNotEqual(status.json['csrf_token'], csrf)
        # A fresh login rotates the server-side session and browser cookie.
        old_ids = set(x['_id'] for x in self.db.ls_vendor_sessions.find())
        self.db.ls_vendor_auth_counters.delete_many({})
        self.login()
        old = self.db.ls_vendor_sessions.find_one({'_id': next(iter(old_ids))})
        self.assertIn('revoked_at', old)

    def test_cookie_flags_and_logout_revocation(self):
        request_result = self.post('/auth/request', {'email': 'vendor@example.test'})
        self.assertEqual(request_result.status_code, 202)
        code = self.mail[-1][1]
        verified = self.post('/auth/verify', {'email': 'vendor@example.test', 'code': code})
        cookie = verified.headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)
        self.assertNotIn('Secure;', cookie)  # Explicit local HTTP test mode only.
        csrf = verified.json['csrf_token']
        self.assertEqual(self.post('/auth/logout', {}, csrf=csrf).status_code, 200)
        self.assertEqual(self.client.get(PREFIX + '/session').status_code, 401)
        self.assertEqual(self.db.ls_vendor_sessions.count_documents({'revoked_at': {'$exists': True}}), 1)

    def test_expired_wrong_reused_code_and_attempt_limit(self):
        self.post('/auth/request', {'email': 'vendor@example.test'})
        code = self.mail[-1][1]
        wrong = self.post('/auth/verify', {'email': 'vendor@example.test', 'code': '00000000'})
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(self.post('/auth/verify', {'email': 'vendor@example.test', 'code': code}).status_code, 200)
        self.assertEqual(self.post('/auth/verify', {'email': 'vendor@example.test', 'code': code}).status_code, 403)
        self.db.ls_vendor_auth_counters.delete_many({})
        self.post('/auth/request', {'email': 'other@example.test'})
        for _ in range(5):
            self.assertEqual(self.post('/auth/verify', {'email': 'other@example.test',
                                                        'code': '99999999'}).status_code, 403)
        self.assertEqual(self.post('/auth/verify', {'email': 'other@example.test',
                                                    'code': self.mail[-1][1]}).status_code, 403)
        self.db.ls_vendor_auth_counters.delete_many({})
        self.post('/auth/request', {'email': 'third@example.test'})
        email_code = self.db.ls_vendor_email_codes.find_one({'email': 'third@example.test'})
        self.db.ls_vendor_email_codes.update_one({'_id': email_code['_id']},
                                                  {'$set': {'expires_at': dt.datetime.utcnow() - dt.timedelta(seconds=1)}})
        self.assertEqual(self.post('/auth/verify', {'email': 'third@example.test',
                                                    'code': self.mail[-1][1]}).status_code, 403)

    def test_request_cooldown_and_generic_response(self):
        first = self.post('/auth/request', {'email': 'vendor@example.test'})
        second = self.post('/auth/request', {'email': 'vendor@example.test'})
        self.assertEqual(first.json, second.json)
        self.assertEqual(len(self.mail), 1)
        self.assertNotIn(self.mail[0][1], str(first.json))

    def test_request_hourly_limit(self):
        for attempt in range(5):
            self.db.ls_vendor_auth_counters.update_many(
                {}, {'$set': {'next_allowed_at': dt.datetime.utcnow() - dt.timedelta(seconds=1)}})
            self.assertEqual(self.post('/auth/request',
                                       {'email': 'vendor@example.test'}).status_code, 202)
        self.db.ls_vendor_auth_counters.update_many(
            {}, {'$set': {'next_allowed_at': dt.datetime.utcnow() - dt.timedelta(seconds=1)}})
        self.assertEqual(self.post('/auth/request',
                                   {'email': 'vendor@example.test'}).status_code, 202)
        self.assertEqual(len(self.mail), 5)

    def test_production_cookie_is_secure(self):
        with patch.dict(os.environ, {'LAPTOPSEAL_TEST_MODE': '0'}):
            self.post('/auth/request', {'email': 'vendor@example.test'})
            code = self.mail[-1][1]
            result = self.post('/auth/verify', {'email': 'vendor@example.test', 'code': code})
            self.assertEqual(result.status_code, 200)
            self.assertIn('Secure', result.headers['Set-Cookie'])

    def test_session_expiry_unauthorized_and_csrf(self):
        self.assertEqual(self.client.get(PREFIX + '/packages').status_code, 401)
        csrf = self.login()
        self.assertEqual(self.post('/auth/logout', {}).status_code, 403)
        self.assertEqual(self.post('/auth/logout', {}, csrf='wrong').status_code, 403)
        self.assertEqual(self.post('/auth/logout', {}, csrf=csrf).status_code, 200)
        self.db.ls_vendor_auth_counters.delete_many({})
        self.login()
        session = self.db.ls_vendor_sessions.find_one({'revoked_at': {'$exists': False}})
        self.db.ls_vendor_sessions.update_one({'_id': session['_id']},
                                               {'$set': {'expires_at': dt.datetime.utcnow() - dt.timedelta(seconds=1)}})
        self.assertEqual(self.client.get(PREFIX + '/session').status_code, 401)

    def test_cross_site_origin_rejected(self):
        csrf = self.login()
        response = self.client.post(PREFIX + '/auth/logout', json={},
                                    headers={'X-LaptopSeal-CSRF': csrf,
                                             'Origin': 'https://attacker.example'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get(PREFIX + '/session').status_code, 200)

    def test_package_listing_ownership_remaining_and_expiry(self):
        self.login()
        vendor = self.db.ls_vendor_identities.find_one({'email': 'vendor@example.test'})
        a = issue_test_package(self.db, vendor['_id'], 'single')
        b = issue_test_package(self.db, vendor['_id'], 'basic')
        other_client = self.client.application.test_client()
        self.login('other@example.test', other_client)
        other = self.db.ls_vendor_identities.find_one({'email': 'other@example.test'})
        issue_test_package(self.db, other['_id'], 'business')
        result = self.client.get(PREFIX + '/packages')
        self.assertEqual(len(result.json['packages']), 2)
        self.assertEqual({p['id'] for p in result.json['packages']}, {a['_id'], b['_id']})
        self.assertEqual(self.client.get(PREFIX + '/packages/' + b['_id']).json['package']['remaining_slots'], 5)
        self.assertEqual(self.client.get(PREFIX + '/packages/' + other_client.get(PREFIX + '/packages').json['packages'][0]['id']).status_code, 404)

    def test_challenge_approval_poll_and_signed_proof(self):
        csrf = self.login()
        vendor = self.db.ls_vendor_identities.find_one({'email': 'vendor@example.test'})
        package = issue_test_package(self.db, vendor['_id'], 'single')
        created = self.post('/challenges', {'hardware_id': DEVICE1})
        self.assertEqual(created.status_code, 201)
        data = created.json
        self.assertNotIn('poll_secret', data['qr'])
        cid = data['challenge_id']
        poll = self.post('/challenges/' + cid + '/poll', {'poll_secret': data['poll_secret']})
        self.assertEqual(poll.json['status'], 'pending')
        self.assertEqual(self.post('/challenges/' + cid + '/poll', {'poll_secret': 'x' * 43}).status_code, 404)
        inspect = self.post('/challenges/' + cid + '/inspect',
                            {'qr_secret': data['qr']['qr_secret']}, csrf=csrf)
        self.assertEqual(inspect.status_code, 200)
        self.assertNotIn('device_hash', inspect.json)
        self.assertEqual(self.post('/challenges/' + cid + '/approve',
                                   {'qr_secret': data['qr']['qr_secret'], 'package_id': package['_id']}).status_code, 403)
        approved = self.post('/challenges/' + cid + '/approve',
                             {'qr_secret': data['qr']['qr_secret'], 'package_id': package['_id']}, csrf=csrf)
        self.assertEqual(approved.status_code, 200)
        self.assertNotIn('entitlement', approved.json)
        self.assertEqual(self.post('/challenges/' + cid + '/approve',
                                   {'qr_secret': data['qr']['qr_secret'], 'package_id': package['_id']}, csrf=csrf).status_code, 409)
        proof = self.post('/challenges/' + cid + '/poll', {'poll_secret': data['poll_secret']})
        self.assertEqual(proof.status_code, 200)
        self.assertTrue(verify_entitlement(proof.json['entitlement'], self.key.public_key(),
                                           DEVICE1, 'test'))
        self.assertEqual(self.client.get(PREFIX + '/packages/' + package['_id']).json['package']['remaining_slots'], 0)
        refreshed = self.post('/entitlements/check',
                              {'hardware_id': DEVICE1, 'entitlement': proof.json['entitlement']})
        self.assertEqual(refreshed.status_code, 200)
        self.assertTrue(verify_entitlement(refreshed.json['entitlement'],
                                           self.key.public_key(), DEVICE1, 'test'))
        self.assertEqual(self.db.ls_vendor_allocations.count_documents({'package_id': package['_id']}), 1)
        self.assertEqual(self.post('/entitlements/check',
                                   {'hardware_id': DEVICE2,
                                    'entitlement': proof.json['entitlement']}).status_code, 403)
        tampered = {'payload': dict(proof.json['entitlement']['payload']),
                    'signature': proof.json['entitlement']['signature']}
        tampered['payload']['expires_at'] = '2099-01-01T00:00:00Z'
        self.assertEqual(self.post('/entitlements/check',
                                   {'hardware_id': DEVICE1, 'entitlement': tampered}).status_code, 403)
        self.db.ls_vendor_packages.update_one({'_id': package['_id']},
                                               {'$set': {'status': 'revoked'}})
        self.assertEqual(self.post('/entitlements/check',
                                   {'hardware_id': DEVICE1,
                                    'entitlement': proof.json['entitlement']}).status_code, 403)

    def test_invalid_device_cross_vendor_and_exhaustion(self):
        csrf = self.login()
        vendor = self.db.ls_vendor_identities.find_one({'email': 'vendor@example.test'})
        package = issue_test_package(self.db, vendor['_id'], 'single')
        self.assertEqual(self.post('/challenges', {'hardware_id': 'random'}).status_code, 400)
        one = self.post('/challenges', {'hardware_id': DEVICE1}).json
        self.db.ls_vendor_auth_counters.delete_many({})
        two = self.post('/challenges', {'hardware_id': DEVICE2}).json
        other_client = self.client.application.test_client()
        other_csrf = self.login('other@example.test', other_client)
        self.assertEqual(self.post('/challenges/' + one['challenge_id'] + '/approve',
                                   {'qr_secret': one['qr']['qr_secret'],
                                    'package_id': package['_id']}, other_client, other_csrf).status_code, 409)
        self.assertEqual(self.post('/challenges/' + one['challenge_id'] + '/approve',
                                   {'qr_secret': one['qr']['qr_secret'],
                                    'package_id': package['_id']}, csrf=csrf).status_code, 200)
        self.assertEqual(self.post('/challenges/' + two['challenge_id'] + '/approve',
                                   {'qr_secret': two['qr']['qr_secret'],
                                    'package_id': package['_id']}, csrf=csrf).status_code, 409)

    def test_expired_challenge_and_package_rejected(self):
        csrf = self.login()
        vendor = self.db.ls_vendor_identities.find_one({'email': 'vendor@example.test'})
        package = issue_test_package(self.db, vendor['_id'], 'single')
        challenge = self.post('/challenges', {'hardware_id': DEVICE1}).json
        self.db.ls_vendor_challenges.update_one({'_id': challenge['challenge_id']},
                                                 {'$set': {'expires_at': dt.datetime.utcnow() - dt.timedelta(seconds=1)}})
        result = self.post('/challenges/' + challenge['challenge_id'] + '/approve',
                           {'qr_secret': challenge['qr']['qr_secret'], 'package_id': package['_id']}, csrf=csrf)
        self.assertEqual(result.status_code, 409)
        self.db.ls_vendor_auth_counters.delete_many({})
        fresh = self.post('/challenges', {'hardware_id': DEVICE1}).json
        self.db.ls_vendor_packages.update_one({'_id': package['_id']},
                                               {'$set': {'expires_at': dt.datetime.utcnow() - dt.timedelta(seconds=1)}})
        result = self.post('/challenges/' + fresh['challenge_id'] + '/approve',
                           {'qr_secret': fresh['qr']['qr_secret'], 'package_id': package['_id']}, csrf=csrf)
        self.assertEqual(result.status_code, 409)

    def test_cancelled_challenge_cannot_be_approved_or_polled_as_active(self):
        csrf = self.login()
        vendor = self.db.ls_vendor_identities.find_one({'email': 'vendor@example.test'})
        package = issue_test_package(self.db, vendor['_id'], 'single')
        challenge = self.post('/challenges', {'hardware_id': DEVICE1}).json
        cancelled = self.post('/challenges/' + challenge['challenge_id'] + '/cancel',
                              {'poll_secret': challenge['poll_secret']})
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(self.post('/challenges/' + challenge['challenge_id'] + '/poll',
                                   {'poll_secret': challenge['poll_secret']}).json['status'], 'cancelled')
        rejected = self.post('/challenges/' + challenge['challenge_id'] + '/approve',
                             {'qr_secret': challenge['qr']['qr_secret'],
                              'package_id': package['_id']}, csrf=csrf)
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(self.db.ls_vendor_allocations.count_documents({}), 0)

    def test_same_device_uses_one_slot_across_two_challenges(self):
        csrf = self.login()
        vendor = self.db.ls_vendor_identities.find_one({'email': 'vendor@example.test'})
        package = issue_test_package(self.db, vendor['_id'], 'single')
        for _ in range(2):
            self.db.ls_vendor_auth_counters.delete_many({})
            challenge = self.post('/challenges', {'hardware_id': DEVICE1}).json
            result = self.post('/challenges/' + challenge['challenge_id'] + '/approve',
                               {'qr_secret': challenge['qr']['qr_secret'],
                                'package_id': package['_id']}, csrf=csrf)
            self.assertEqual(result.status_code, 200)
        self.assertEqual(self.db.ls_vendor_allocations.count_documents({'package_id': package['_id']}), 1)


if __name__ == '__main__':
    unittest.main()
