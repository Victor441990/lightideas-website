"""Isolated Flask route tests: no production DB, Paystack, email, or customers."""
import datetime
import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import mongomock
from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class LaptopSealFlaskIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = mongomock.MongoClient()
        environment = {
            'MONGO_URI': 'mongodb://127.0.0.1:1/isolation-only',
            'FLASK_SECRET_KEY': 'synthetic-flask-secret',
            'ADMIN_PASSWORD_HASH': generate_password_hash('synthetic-admin-password'),
            'LAPTOPSEAL_LICENSE_PEPPER': 'synthetic-license-pepper',
            'PAYSTACK_SECRET_KEY': 'synthetic-paystack-secret',
            'PAYSTACK_PUBLIC_KEY': 'pk_test_synthetic',
            'BREVO_API_KEY': 'synthetic-brevo-key',
            'LAPTOPSEAL_DELIVERY_KEY': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
        }
        cls.env_patch = patch.dict(os.environ, environment, clear=True)
        cls.env_patch.start()
        cls.mongo_patch = patch('pymongo.MongoClient', return_value=cls.mongo)
        cls.mongo_patch.start()
        cls.backend = importlib.import_module('app')
        cls.backend.app.config['TESTING'] = True
        cls.backend.ensure_laptopseal_security_indexes()

    @classmethod
    def tearDownClass(cls):
        cls.mongo_patch.stop()
        cls.env_patch.stop()

    def setUp(self):
        self.db = self.mongo['lightideas']
        for name in self.db.list_collection_names():
            self.db[name].delete_many({})
        self.client = self.backend.app.test_client()

    def test_production_database_default_is_lightideas(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('MONGO_DB_NAME', None)
            os.environ.pop('LAPTOPSEAL_TEST_MODE', None)
            self.assertEqual(self.backend.configured_mongo_db_name(), 'lightideas')
            self.assertEqual(self.backend.get_db().name, 'lightideas')

    def test_vendor_portal_assets_load_without_authentication(self):
        page = self.client.get('/laptopseal/vendor')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Verify your email', page.data)
        self.assertIn(b'Scan the laptop', page.data)
        self.assertEqual(self.client.get('/static/js/laptopseal-vendor.js').status_code, 200)
        self.assertEqual(self.client.get('/static/js/jsQR-1.4.0.js').status_code, 200)
        self.assertEqual(self.client.get('/static/css/laptopseal-vendor.css').status_code, 200)

    def test_payment_test_mode_requires_non_production_database(self):
        with patch.dict(os.environ, {
                'LAPTOPSEAL_TEST_MODE': '1', 'MONGO_DB_NAME': 'laptopseal_payment_test'}, clear=False):
            self.assertIsNone(self.backend.laptopseal_test_database_error())
            self.assertEqual(self.backend.get_db().name, 'laptopseal_payment_test')

    def test_payment_test_mode_rejects_production_database(self):
        with patch.dict(os.environ, {
                'LAPTOPSEAL_TEST_MODE': '1', 'MONGO_DB_NAME': 'lightideas',
                'LAPTOPSEAL_PAYMENTS_ENABLED': '1'}, clear=False):
            response = self.client.post('/laptopseal/initialize_payment', json={
                'email': 'buyer@example.test', 'plan_id': 'single'})
        self.assertEqual(response.status_code, 503)
        self.assertIn('production database', response.json['error'])

    def test_missing_test_database_configuration_prevents_checkout(self):
        with patch.dict(os.environ, {
                'LAPTOPSEAL_TEST_MODE': '1', 'MONGO_DB_NAME': '',
                'LAPTOPSEAL_PAYMENTS_ENABLED': '1'}, clear=False):
            response = self.client.post('/laptopseal/initialize_payment', json={
                'email': 'buyer@example.test', 'plan_id': 'single'})
        self.assertEqual(response.status_code, 503)
        self.assertIn('MONGO_DB_NAME is required', response.json['error'])

    def login_admin(self):
        with self.client.session_transaction() as session:
            session['admin_logged_in'] = True
            session['csrf_token'] = 'synthetic-admin-csrf'

    def owner_key(self):
        self.login_admin()
        key = 'Synthetic permanent owner password 2026'
        response = self.client.post('/api/laptopseal/owner-master-key', json={
            'admin_password': 'synthetic-admin-password', 'master_key': key},
            headers={'X-CSRF-Token': 'synthetic-admin-csrf'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['success'])
        return key

    def test_laptopseal_admin_write_requires_csrf_and_hash_reauthentication(self):
        self.login_admin()
        page = self.client.get('/victor-admin/laptopseal')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'name="csrf-token"', page.data)
        self.assertIn(b'X-CSRF-Token', page.data)
        payload = {'admin_password': 'synthetic-admin-password',
                   'master_key': 'Synthetic owner phrase for isolated test'}
        self.assertEqual(self.client.post('/api/laptopseal/owner-master-key',
                                          json=payload).status_code, 400)
        payload['admin_password'] = 'wrong-synthetic-password'
        self.assertEqual(self.client.post('/api/laptopseal/owner-master-key',
                                          json=payload,
                                          headers={'X-CSRF-Token': 'synthetic-admin-csrf'}).status_code, 403)
        self.assertEqual(self.db['ls_owner_master_key'].count_documents({}), 0)

    def test_checkout_disabled_and_no_paystack_call(self):
        with patch.object(self.backend.requests, 'get') as paystack:
            response = self.client.post('/laptopseal/verify_payment', json={
                'reference': 'synthetic-ref-001', 'email': 'buyer@example.test',
                'plan_id': 'single'})
        self.assertEqual(response.status_code, 403)
        paystack.assert_not_called()
        self.assertEqual(self.db['ls_licenses'].count_documents({}), 0)

    def test_owner_multiple_devices_30_days_renewal_expiry_and_revoke(self):
        key = self.owner_key()
        activations = []
        for number in range(12):
            result = self.client.post('/laptopseal/owner/activate', json={
                'master_key': key, 'hardware_id': f'owner-device-{number}'})
            self.assertEqual(result.status_code, 200)
            activations.append(result.json['owner_token'])
        self.assertEqual(len(set(activations)), 12)
        self.assertEqual(self.db['ls_owner_device_activations'].count_documents({}), 12)
        self.assertEqual(self.db['ls_owner_key_attempts'].count_documents({}), 0)
        for doc in self.db['ls_owner_device_activations'].find():
            self.assertEqual(doc['expires_at'] - doc['activated_at'], datetime.timedelta(days=30))
        self.assertEqual(self.db['ls_licenses'].count_documents({}), 0)
        first = self.client.post('/laptopseal/owner/check', json={
            'owner_token': activations[0], 'hardware_id': 'owner-device-0'})
        self.assertTrue(first.json['valid'])
        swapped = self.client.post('/laptopseal/owner/check', json={
            'owner_token': activations[0], 'hardware_id': 'owner-device-1'})
        self.assertFalse(swapped.json['valid'])
        identity = self.backend.opaque_id('owner-device-0', self.backend.LAPTOPSEAL_LICENSE_PEPPER)
        self.db['ls_owner_device_activations'].update_one({'_id': identity}, {'$set': {
            'expires_at': datetime.datetime.utcnow() - datetime.timedelta(seconds=1)}})
        expired = self.client.post('/laptopseal/owner/check', json={
            'owner_token': activations[0], 'hardware_id': 'owner-device-0'})
        self.assertFalse(expired.json['valid'])
        renewed = self.client.post('/laptopseal/owner/activate', json={
            'master_key': key, 'hardware_id': 'owner-device-0'})
        self.assertTrue(renewed.json['success'])
        self.assertNotEqual(renewed.json['owner_token'], activations[0])
        self.assertTrue(self.client.post('/laptopseal/owner/check', json={
            'owner_token': renewed.json['owner_token'], 'hardware_id': 'owner-device-0'}).json['valid'])
        revoke = self.client.post('/api/laptopseal/owner-master-key/revoke', json={
            'admin_password': 'synthetic-admin-password'},
            headers={'X-CSRF-Token': 'synthetic-admin-csrf'})
        self.assertTrue(revoke.json['success'])
        self.assertFalse(self.client.post('/laptopseal/owner/check', json={
            'owner_token': renewed.json['owner_token'], 'hardware_id': 'owner-device-0'}).json['valid'])

    def test_unified_activation_routes_customer_and_owner_without_client_key_type(self):
        owner_key = self.owner_key()
        customer_key = 'synthetic-unified-customer-token'
        self.db['ls_licenses'].insert_one({
            'token': customer_key, 'active': True, 'tier': 'single',
            'email': 'buyer@example.test', 'max_devices': 1, 'devices': [],
            'expires_at': datetime.datetime.utcnow() + datetime.timedelta(days=30)})

        customer = self.client.post('/laptopseal/activate_key', json={
            'key': customer_key, 'hardware_id': 'customer-device'})
        self.assertTrue(customer.json['success'])
        self.assertNotIn('owner_token', customer.json)
        license_doc = self.db['ls_licenses'].find_one({'token': customer_key})
        self.assertEqual(license_doc['devices'], ['customer-device'])
        repeated = self.client.post('/laptopseal/activate_key', json={
            'key': customer_key, 'hardware_id': 'customer-device'})
        self.assertTrue(repeated.json['success'])
        self.assertEqual(self.db['ls_licenses'].find_one({'token': customer_key})['devices'],
                         ['customer-device'])
        self.assertEqual(self.db['ls_owner_device_activations'].count_documents({}), 0)

        owner = self.client.post('/laptopseal/activate_key', json={
            'key': owner_key, 'hardware_id': 'owner-device'})
        self.assertTrue(owner.json['success'])
        self.assertTrue(owner.json.get('owner_token'))
        self.assertTrue(self.client.post('/laptopseal/owner/check', json={
            'owner_token': owner.json['owner_token'], 'hardware_id': 'owner-device'}).json['valid'])

        at_limit = self.client.post('/laptopseal/activate_key', json={
            'key': customer_key, 'hardware_id': 'second-customer-device'})
        self.assertFalse(at_limit.json['success'])
        self.assertEqual(at_limit.json['error'], 'Activation could not be completed.')
        self.assertEqual(self.db['ls_licenses'].find_one({'token': customer_key})['devices'],
                         ['customer-device'])

        expired_key = 'synthetic-expired-customer-token'
        self.db['ls_licenses'].insert_one({
            'token': expired_key, 'active': True, 'tier': 'single',
            'email': 'buyer@example.test', 'max_devices': 1, 'devices': [],
            'expires_at': datetime.datetime.utcnow() - datetime.timedelta(seconds=1)})
        attempts_before = self.db['ls_owner_key_attempts'].count_documents({})
        expired = self.client.post('/laptopseal/activate_key', json={
            'key': expired_key, 'hardware_id': 'expired-device'})
        self.assertFalse(expired.json['success'])
        self.assertEqual(expired.json['error'], 'Activation could not be completed.')
        self.assertEqual(self.db['ls_owner_key_attempts'].count_documents({}), attempts_before)

        invalid = self.client.post('/laptopseal/activate_key', json={
            'key': 'invalid-synthetic-key', 'hardware_id': 'invalid-device'})
        self.assertFalse(invalid.json['success'])
        self.assertEqual(invalid.json['error'], 'Activation could not be completed.')

    def test_invalid_owner_key_rate_limit(self):
        key = self.owner_key()
        for _ in range(5):
            self.assertEqual(self.client.post('/laptopseal/owner/activate', json={
                'master_key': 'wrong synthetic owner password', 'hardware_id': 'device-a'}).status_code, 403)
        self.assertEqual(self.client.post('/laptopseal/owner/activate', json={
            'master_key': key, 'hardware_id': 'device-a'}).status_code, 429)

    def test_distributed_bad_guesses_share_global_budget(self):
        self.owner_key()
        for number in range(3):
            response = self.client.post('/laptopseal/owner/activate', json={
                'master_key': 'wrong synthetic owner password',
                'hardware_id': f'distributed-device-{number}'},
                environ_overrides={'REMOTE_ADDR': f'198.51.100.{number + 1}'})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.db['ls_owner_key_attempts'].count_documents({
            '_id': {'$regex': '^global:'}}), 1)
        self.assertEqual(self.db['ls_owner_key_attempts'].find_one({
            '_id': {'$regex': '^global:'}})['count'], 3)
        self.db['ls_owner_key_attempts'].update_one({'_id': {'$regex': '^global:'}},
            {'$set': {'count': self.backend.OWNER_RATE_MAX_ATTEMPTS * 20}})
        blocked = self.client.post('/laptopseal/owner/activate', json={
            'master_key': 'wrong synthetic owner password',
            'hardware_id': 'new-device'},
            environ_overrides={'REMOTE_ADDR': '203.0.113.10'})
        self.assertEqual(blocked.status_code, 429)

    def verified_transaction(self, plan_id='single', reference='synthetic-ref-001'):
        plan = self.backend.PLANS[plan_id]
        return {'reference': reference, 'status': 'success', 'currency': 'NGN',
                'amount': plan['amount'], 'customer': {'email': 'buyer@example.test'},
                'metadata': {'product_id': 'laptopseal', 'plan_id': plan_id}}

    def checkout(self, transaction, requested_plan='single', email_ok=True):
        with patch.dict(os.environ, {'LAPTOPSEAL_PAYMENTS_ENABLED': '1'}):
            intent = self.client.post('/laptopseal/initialize_payment', json={
                'email': 'buyer@example.test', 'plan_id': requested_plan})
        self.assertEqual(intent.status_code, 200)
        transaction = dict(transaction, reference=intent.json['reference'])
        self.last_reference = transaction['reference']
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'status': True, 'data': transaction}
        with patch.dict(os.environ, {'LAPTOPSEAL_PAYMENTS_ENABLED': '1'}), patch.object(
                self.backend.requests, 'get', return_value=response), patch.object(
                self.backend, 'send_brevo_email', return_value=email_ok):
            return self.client.post('/laptopseal/verify_payment', json={
                'reference': transaction['reference']})

    def test_verified_payment_once_price_plan_and_device_limit(self):
        transaction = self.verified_transaction()
        bad_amount = dict(transaction, amount=transaction['amount'] - 1)
        self.assertEqual(self.checkout(bad_amount).status_code, 400)
        self.assertEqual(self.checkout(transaction, 'reseller_5').status_code, 400)
        paid = self.checkout(transaction)
        self.assertEqual(paid.status_code, 200)
        self.assertEqual(paid.json['max_devices'], 1)
        self.assertEqual(self.db['ls_licenses'].count_documents({}), 1)
        license_doc = self.db['ls_licenses'].find_one({'reference': self.last_reference})
        issuance_audit = self.db['ls_license_audit'].find_one({'reference': self.last_reference})
        self.assertEqual(issuance_audit['event'], 'customer_license_issued')
        self.assertEqual(issuance_audit['license_id'], str(license_doc['_id']))
        self.assertNotIn('token', issuance_audit)
        token = self.backend._decrypt_delivery_token(license_doc['delivery_token_encrypted'])
        self.assertNotIn('token', self.db['ls_licenses'].find_one({'reference': self.last_reference}))
        duplicate_response = Mock()
        duplicate_response.raise_for_status.return_value = None
        duplicate_response.json.return_value = {'status': True, 'data': dict(
            transaction, reference=self.last_reference)}
        with patch.dict(os.environ, {'LAPTOPSEAL_PAYMENTS_ENABLED': '1'}), patch.object(
                self.backend.requests, 'get', return_value=duplicate_response), patch.object(
                self.backend, 'send_brevo_email') as delivery:
            duplicate = self.client.post('/laptopseal/verify_payment', json={
                'reference': self.last_reference})
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json['already_fulfilled'])
        self.assertEqual(self.db['ls_licenses'].count_documents({'reference': self.last_reference}), 1)
        self.assertEqual(self.db['ls_license_audit'].count_documents({'reference': self.last_reference}), 1)
        delivery.assert_not_called()
        self.assertTrue(self.client.post('/laptopseal/bind_hardware', json={
            'token': token, 'hardware_id': 'customer-device-1'}).json['success'])
        self.assertFalse(self.client.post('/laptopseal/bind_hardware', json={
            'token': token, 'hardware_id': 'customer-device-2'}).json['success'])
        checked = self.client.post('/laptopseal/check_license', json={
            'token': token, 'hardware_id': 'customer-device-1'}).json
        self.assertTrue(checked['valid'])
        self.assertIn('expires_at_utc', checked)
        self.assertIn('server_time_utc', checked)
        self.assertFalse(self.client.post('/laptopseal/check_license', json={
            'token': token, 'hardware_id': 'customer-device-2'}).json['valid'])
        self.db['ls_licenses'].update_one({'reference': self.last_reference}, {'$set': {
            'expires_at': datetime.datetime.utcnow() - datetime.timedelta(seconds=1)}})
        self.assertFalse(self.client.post('/laptopseal/check_license', json={
            'token': token, 'hardware_id': 'customer-device-1'}).json['valid'])

    def test_customer_license_revocation_is_reauthenticated_audited_and_discovered(self):
        transaction = self.verified_transaction(reference='synthetic-revoke-001')
        paid = self.checkout(transaction)
        reference = self.last_reference
        token = self.backend._decrypt_delivery_token(self.db['ls_licenses'].find_one(
            {'reference': reference})['delivery_token_encrypted'])
        self.assertTrue(self.client.post('/laptopseal/bind_hardware', json={
            'token': token, 'hardware_id': 'customer-device-1'}).json['success'])

        # A session alone, a cross-site browser request, or a bad reference may
        # not revoke a paid license.
        self.assertEqual(self.client.post('/api/laptopseal/licenses/revoke', json={
            'reference': reference, 'admin_password': 'synthetic-admin-password'}).status_code, 401)
        self.login_admin()
        denied = self.client.post('/api/laptopseal/licenses/revoke', json={
            'reference': reference, 'admin_password': 'synthetic-admin-password'},
            headers={'Sec-Fetch-Site': 'cross-site',
                     'X-CSRF-Token': 'synthetic-admin-csrf'})
        self.assertEqual(denied.status_code, 403)

        revoked = self.client.post('/api/laptopseal/licenses/revoke', json={
            'reference': reference, 'admin_password': 'synthetic-admin-password'},
            headers={'X-CSRF-Token': 'synthetic-admin-csrf'})
        self.assertEqual(revoked.status_code, 200)
        self.assertTrue(revoked.json['success'])
        self.assertFalse(self.client.post('/laptopseal/check_license', json={
            'token': token, 'hardware_id': 'customer-device-1'}).json['valid'])
        self.assertFalse(self.client.post('/laptopseal/bind_hardware', json={
            'token': token, 'hardware_id': 'customer-device-1'}).json['success'])
        audit = self.db['ls_license_audit'].find_one({
            'reference': reference, 'event': 'customer_license_revoked'})
        self.assertEqual(audit['event'], 'customer_license_revoked')
        self.assertNotIn(token, str(audit))

    def test_admin_license_data_does_not_expose_plaintext_token(self):
        transaction = self.verified_transaction(reference='synthetic-admin-data-001')
        self.checkout(transaction)
        token = self.backend._decrypt_delivery_token(self.db['ls_licenses'].find_one(
            {'reference': self.last_reference})['delivery_token_encrypted'])
        self.login_admin()
        response = self.client.get('/laptopseal/admin_data')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(token, response.get_data(as_text=True))
        self.assertNotIn('token"', response.get_data(as_text=True))

    def test_delivery_page_and_email_recovery_reuse_one_encrypted_license(self):
        transaction = self.verified_transaction(reference='synthetic-delivery-001')
        paid = self.checkout(transaction, email_ok=False)
        self.assertTrue(paid.json['success'])
        reference = self.last_reference
        license_doc = self.db['ls_licenses'].find_one({'reference': reference})
        token = self.backend._decrypt_delivery_token(license_doc['delivery_token_encrypted'])
        self.assertEqual(license_doc['delivery_status'], 'pending')
        page = self.client.get('/laptopseal/activate')
        self.assertIn(token, page.get_data(as_text=True))
        self.assertNotIn(token, page.headers.get('Location', ''))
        self.assertEqual(page.headers['Cache-Control'], 'no-store')
        with patch.object(self.backend, 'send_brevo_email', return_value=True) as delivery:
            recovery = self.client.post('/laptopseal/recover-license', json={
                'reference': reference, 'email': 'buyer@example.test'})
        self.assertTrue(recovery.json['success'])
        delivery.assert_called_once()
        self.assertEqual(self.db['ls_licenses'].count_documents({'reference': reference}), 1)
        self.assertEqual(self.db['ls_licenses'].find_one({'reference': reference})['delivery_status'], 'sent')

    def test_invalid_payment_does_not_issue_license(self):
        cases = [
            dict(self.verified_transaction(), status='failed'),
            dict(self.verified_transaction(), currency='USD'),
            dict(self.verified_transaction(), customer={'email': 'other@example.test'}),
            dict(self.verified_transaction(), metadata={'product_id': 'other', 'plan_id': 'single'}),
        ]
        for transaction in cases:
            with self.subTest(transaction=transaction['status']):
                self.assertEqual(self.checkout(transaction).status_code, 400)
        self.assertEqual(self.db['ls_licenses'].count_documents({}), 0)

    def test_admin_announcement_public_poll_and_ack_dedup(self):
        self.assertEqual(self.client.post('/api/announcements', json={
            'title': 'Synthetic notice', 'body': 'Local test only'}).status_code, 401)
        self.login_admin()
        with self.client.session_transaction() as session:
            session['csrf_token'] = 'synthetic-announcement-csrf'
        created = self.client.post('/api/announcements', json={
            'title': 'Synthetic notice', 'body': 'Local test only',
            'link': 'example.test', 'active': True},
            headers={'X-CSRF-Token': 'synthetic-announcement-csrf'})
        self.assertEqual(created.status_code, 200)
        version = created.json['announcement']['id']
        polled = self.client.get('/api/laptopseal/notification')
        self.assertEqual(polled.json['version'], version)
        self.assertEqual(polled.json['link'], 'https://example.test')
        for _ in range(2):
            ack = self.client.post('/api/laptopseal/ack', json={
                'device_id': 'synthetic-device', 'version': version})
            self.assertTrue(ack.json['success'])
        self.assertEqual(self.db['ls_announcement_acks'].count_documents({
            'announcement_id': version, 'device_id': 'synthetic-device'}), 1)


if __name__ == '__main__':
    unittest.main()
