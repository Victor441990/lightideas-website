"""Vendor checkout uses in-memory MongoDB and mocked Paystack only."""

import datetime as dt
import hashlib
import hmac
import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import mongomock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from laptopseal_vendor import ensure_vendor_indexes


SECRET = 'sk_test_synthetic_vendor_secret'
EMAIL = 'buyer@example.test'


class VendorPaystackPurchaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {
            'MONGO_URI': 'mongodb://127.0.0.1:1/mock-only',
            'MONGO_DB_NAME': 'laptopseal_test', 'LAPTOPSEAL_TEST_MODE': '1',
            'LAPTOPSEAL_VENDOR_PAYMENTS_ENABLED': '1',
            'LAPTOPSEAL_PAYMENTS_ENABLED': '0',
            'PAYSTACK_PUBLIC_KEY': 'pk_test_synthetic_vendor_public',
            'PAYSTACK_SECRET_KEY': SECRET,
            'BREVO_API_KEY': 'synthetic-brevo-key',
            'FLASK_SECRET_KEY': 'synthetic-vendor-session',
            'LAPTOPSEAL_LICENSE_PEPPER': 'synthetic-vendor-pepper-long-enough',
            'LAPTOPSEAL_VENDOR_TEST_CODE_CAPTURE': '1',
        })
        cls.env.start()
        cls.mongo = mongomock.MongoClient()
        with patch('pymongo.MongoClient', return_value=cls.mongo):
            cls.backend = importlib.import_module('app')
        cls.brevo_patch = patch.object(cls.backend, 'BREVO_API_KEY', 'synthetic-brevo-key')
        cls.brevo_patch.start()
        cls.mongo_patch = patch.object(cls.backend, '_mongo_client', cls.mongo)
        cls.mongo_patch.start()
        cls.webhook_secret_patch = patch.object(cls.backend, 'PAYSTACK_WEBHOOK_SECRET', SECRET)
        cls.webhook_secret_patch.start()
        cls.backend.app.secret_key = 'synthetic-vendor-session'
        cls.backend.app.config['TESTING'] = True
        cls.db = cls.mongo['laptopseal_test']
        cls.backend.ensure_laptopseal_security_indexes()
        ensure_vendor_indexes(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.webhook_secret_patch.stop()
        cls.mongo_patch.stop()
        cls.brevo_patch.stop()
        cls.env.stop()

    def setUp(self):
        for name in self.db.list_collection_names():
            self.db[name].delete_many({})
        self.client = self.backend.app.test_client()

    def intent(self, plan='single', email=EMAIL):
        result = self.client.post('/laptopseal/vendor/initialize_payment',
                                  json={'email': email, 'plan_id': plan})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['amount'],
                         self.backend.VENDOR_PLANS[plan]['amount_kobo'])
        return result.json['reference']

    def transaction(self, intent_reference, plan='single', **changes):
        paid = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=2)).isoformat()
        value = {'reference': intent_reference, 'status': 'success', 'currency': 'NGN',
                 'amount': self.backend.VENDOR_PLANS[plan]['amount_kobo'],
                 'paid_at': paid, 'customer': {'email': EMAIL},
                 'metadata': {'product_id': 'laptopseal_vendor', 'plan_id': plan}}
        value.update(changes)
        return value

    def verify(self, reference, transaction):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'status': True, 'data': transaction}
        with patch.object(self.backend.requests, 'get', return_value=response) as paystack, \
             patch.object(self.backend, 'send_brevo_email') as sender:
            result = self.client.post('/laptopseal/vendor/verify_payment',
                                      json={'reference': reference})
        sender.assert_not_called()
        if result.status_code == 200:
            paystack.assert_called_once()
        return result

    def webhook(self, reference, transaction):
        body = json.dumps({'id': 'event-' + reference, 'event': 'charge.success',
                           'data': {'reference': reference}}).encode()
        signature = hmac.new(SECRET.encode(), body, hashlib.sha512).hexdigest()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'status': True, 'data': transaction}
        with patch.object(self.backend.requests, 'get', return_value=response), \
             patch.object(self.backend, 'send_brevo_email') as sender:
            result = self.client.post('/webhooks/paystack/laptopseal', data=body,
                content_type='application/json', headers={'x-paystack-signature': signature})
        sender.assert_not_called()
        return result

    def test_purchase_issues_one_package_from_verified_paid_time(self):
        ref = self.intent('basic', ' BUYER@EXAMPLE.TEST ')
        paid_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
        transaction = self.transaction(ref, 'basic', paid_at=paid_at.isoformat())
        result = self.verify(ref, transaction)
        self.assertEqual(result.status_code, 200, result.json)
        package = self.db.ls_vendor_packages.find_one({'purchase_reference': ref})
        self.assertEqual((package['plan_id'], package['max_devices'], package['amount_kobo']),
                         ('basic', 5, 800000))
        self.assertEqual(package['expires_at'] - package['issued_at'], dt.timedelta(days=30))
        self.assertEqual(package['issued_at'], paid_at.replace(
            tzinfo=None, microsecond=(paid_at.microsecond // 1000) * 1000))
        self.assertEqual(package['delivery_status'], 'skipped_test')
        self.assertEqual(self.db.ls_vendor_identities.find_one(
            {'_id': package['vendor_id']})['email'], EMAIL)
        self.assertEqual(self.db.ls_payment_intents.find_one({'reference': ref})['state'],
                         'fulfilled')
        self.assertEqual(self.client.get('/laptopseal/vendor/confirmation').status_code, 200)

    def test_each_vendor_offer_uses_its_own_server_price_and_capacity(self):
        for plan, amount, capacity in (('single', 200000, 1), ('basic', 800000, 5),
                                       ('business', 3000000, 20),
                                       ('unlimited', 6000000, 200)):
            with self.subTest(plan=plan):
                reference = self.intent(plan)
                self.assertEqual(self.verify(reference, self.transaction(
                    reference, plan)).status_code, 200)
                package = self.db.ls_vendor_packages.find_one(
                    {'purchase_reference': reference})
                self.assertEqual((package['amount_kobo'], package['max_devices']),
                                 (amount, capacity))
        self.assertEqual(self.db.ls_vendor_packages.count_documents({}), 4)

    def test_duplicate_callback_webhook_and_interrupted_intent_repair(self):
        ref = self.intent()
        transaction = self.transaction(ref)
        self.assertEqual(self.verify(ref, transaction).status_code, 200)
        with patch.object(self.backend.requests, 'get') as paystack:
            repeat = self.client.post('/laptopseal/vendor/verify_payment',
                                      json={'reference': ref})
        self.assertTrue(repeat.json['already_fulfilled'])
        paystack.assert_not_called()
        self.assertEqual(self.webhook(ref, transaction).status_code, 200)
        self.assertEqual(self.webhook(ref, transaction).status_code, 200)
        self.assertEqual(self.db.ls_vendor_packages.count_documents({'purchase_reference': ref}), 1)
        self.assertEqual(self.db.ls_paystack_events.count_documents({}), 1)
        self.db.ls_payment_intents.update_one({'reference': ref},
                                              {'$set': {'state': 'pending'}})
        repaired = self.verify(ref, transaction)
        self.assertTrue(repaired.json['already_fulfilled'])
        self.assertEqual(self.db.ls_payment_intents.find_one({'reference': ref})['state'],
                         'fulfilled')
        self.assertEqual(self.db.ls_vendor_packages.count_documents({'purchase_reference': ref}), 1)

    def test_signed_webhook_can_fulfill_without_browser_callback(self):
        ref = self.intent()
        transaction = self.transaction(ref)
        self.assertEqual(self.webhook(ref, transaction).status_code, 200)
        self.assertEqual(self.db.ls_vendor_packages.count_documents({'purchase_reference': ref}), 1)
        repeat = self.client.post('/laptopseal/vendor/verify_payment', json={'reference': ref})
        self.assertTrue(repeat.json['already_fulfilled'])

    def test_invalid_payment_details_issue_no_package(self):
        for change in ({'status': 'failed'}, {'currency': 'USD'},
                       {'amount': 199999}, {'customer': {'email': 'other@example.test'}},
                       {'metadata': {'product_id': 'laptopseal', 'plan_id': 'single'}},
                       {'reference': 'other-reference-0001'}, {'paid_at': None}):
            with self.subTest(change=change):
                ref = self.intent()
                result = self.verify(ref, self.transaction(ref, **change))
                self.assertEqual(result.status_code, 400)
        self.assertEqual(self.db.ls_vendor_packages.count_documents({}), 0)

    def test_multiple_purchases_remain_separate_and_portal_is_email_bound(self):
        first = self.intent('single')
        self.assertEqual(self.verify(first, self.transaction(first)).status_code, 200)
        second = self.intent('business')
        self.assertEqual(self.verify(second, self.transaction(second, 'business')).status_code, 200)
        self.assertEqual(self.db.ls_vendor_packages.count_documents({}), 2)
        self.assertEqual(self.client.get('/api/laptopseal/vendor/packages').status_code, 401)
        captured = []
        with patch('laptopseal_vendor_api.Path.write_text',
                   side_effect=lambda content, **kwargs: captured.append(content)), \
             patch('laptopseal_vendor_api.os.chmod'):
            requested = self.client.post('/api/laptopseal/vendor/auth/request',
                                         json={'email': EMAIL})
        self.assertEqual(requested.status_code, 202)
        code = captured[-1].splitlines()[1]
        verified = self.client.post('/api/laptopseal/vendor/auth/verify',
                                    json={'email': EMAIL, 'code': code})
        self.assertEqual(verified.status_code, 200)
        listing = self.client.get('/api/laptopseal/vendor/packages')
        self.assertEqual({p['plan_id'] for p in listing.json['packages']},
                         {'single', 'business'})

    def test_checkout_gate_test_keys_indexes_and_webhook_signature(self):
        with patch.dict(os.environ, {'LAPTOPSEAL_VENDOR_PAYMENTS_ENABLED': '0'}):
            page = self.client.get('/laptopseal/vendor/purchase')
            self.assertEqual(page.status_code, 200)
            self.assertIn(b'Vendor checkout is currently disabled', page.data)
            self.assertNotIn(b'js.paystack.co', page.data)
            self.assertEqual(self.client.post('/laptopseal/vendor/initialize_payment',
                             json={'email': EMAIL, 'plan_id': 'single'}).status_code, 403)
        enabled_page = self.client.get('/laptopseal/vendor/purchase')
        self.assertIn(b'js.paystack.co', enabled_page.data)
        self.assertIn(b'60,000', enabled_page.data)
        selected = self.client.get('/laptopseal/vendor/purchase?plan=business')
        self.assertIn(b'value="business" selected', selected.data)
        invalid = self.client.get('/laptopseal/vendor/purchase?plan=unknown')
        self.assertIn(b'value="single" selected', invalid.data)
        self.assertEqual(self.client.post('/laptopseal/vendor/initialize_payment',
            json={'email': EMAIL, 'plan_id': 'single'},
            headers={'Origin': 'https://unrelated.example.test'}).status_code, 403)
        with patch.dict(os.environ, {'PAYSTACK_SECRET_KEY': 'sk_live_wrong'}):
            self.assertEqual(self.client.post('/laptopseal/vendor/initialize_payment',
                             json={'email': EMAIL, 'plan_id': 'single'}).status_code, 503)
        self.db.ls_vendor_packages.drop_index('vendor_purchase_unique')
        self.assertEqual(self.client.post('/laptopseal/vendor/initialize_payment',
                         json={'email': EMAIL, 'plan_id': 'single'}).status_code, 503)
        ensure_vendor_indexes(self.db)
        ref = self.intent()
        with patch.object(self.backend.requests, 'get') as paystack:
            bad = self.client.post('/webhooks/paystack/laptopseal',
                data=json.dumps({'event': 'charge.success', 'data': {'reference': ref}}),
                content_type='application/json', headers={'x-paystack-signature': 'invalid'})
        self.assertEqual(bad.status_code, 401)
        paystack.assert_not_called()
        self.assertEqual(self.db.ls_vendor_packages.count_documents({}), 0)

    def test_production_email_notice_uses_existing_sender_without_a_key(self):
        package = {'plan_id': 'business', 'max_devices': 20,
                   'amount_kobo': 3000000, 'issued_at': dt.datetime(2026, 11, 21),
                   'expires_at': dt.datetime(2027, 1, 1),
                   'purchase_reference': 'LSV_synthetic_reference_123'}
        with patch.dict(os.environ, {'LAPTOPSEAL_TEST_MODE': '0'}), \
             patch.object(self.backend, 'send_brevo_email', return_value=True) as sender:
            self.assertTrue(self.backend._send_vendor_package_email(package, EMAIL))
        sender.assert_called_once()
        self.assertIn('laptopseal/vendor', sender.call_args.args[2])
        self.assertIn('20 laptops', sender.call_args.args[3])
        self.assertIn('₦30,000', sender.call_args.args[3])
        self.assertIn('21 November 2026', sender.call_args.args[3])
        self.assertIn('Scan the laptop QR code', sender.call_args.args[3])
        self.assertNotIn('activation key', sender.call_args.args[3].lower())

    def test_confirmation_email_failure_preserves_issued_package(self):
        reference = self.intent()
        intent = self.db.ls_payment_intents.find_one({'reference': reference})
        with patch.dict(os.environ, {'LAPTOPSEAL_TEST_MODE': '0'}), \
             patch.object(self.backend, 'send_brevo_email', return_value=False) as sender:
            outcome = self.backend._fulfill_verified_vendor_payment(
                intent, self.transaction(reference))
        self.assertTrue(outcome['success'])
        sender.assert_called_once()
        package = self.db.ls_vendor_packages.find_one({'purchase_reference': reference})
        self.assertEqual(package['delivery_status'], 'pending')
        self.assertEqual(self.db.ls_vendor_packages.count_documents(
            {'purchase_reference': reference}), 1)

    def test_failed_email_retry_sends_same_notice_once(self):
        reference = self.intent()
        intent = self.db.ls_payment_intents.find_one({'reference': reference})
        transaction = self.transaction(reference)
        with patch.dict(os.environ, {'LAPTOPSEAL_TEST_MODE': '0'}), \
             patch.object(self.backend, 'send_brevo_email', side_effect=[False, True]) as sender, \
             patch.object(self.backend.requests, 'get') as paystack, \
             patch.object(self.backend, '_vendor_payment_configuration_error', return_value=None):
            self.backend._fulfill_verified_vendor_payment(intent, transaction)
            self.assertEqual(self.db.ls_vendor_packages.find_one(
                {'purchase_reference': reference})['delivery_status'], 'pending')
            repeat = self.client.post('/laptopseal/vendor/verify_payment',
                                      json={'reference': reference})
            self.assertEqual(repeat.status_code, 200, repeat.json)
            self.assertTrue(repeat.json['already_fulfilled'])
            repeat = self.client.post('/laptopseal/vendor/verify_payment',
                                      json={'reference': reference})
            self.assertEqual(repeat.status_code, 200, repeat.json)
            self.assertTrue(repeat.json['already_fulfilled'])
        paystack.assert_not_called()
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(self.db.ls_vendor_packages.find_one(
            {'purchase_reference': reference})['delivery_status'], 'sent')
        self.assertEqual(self.db.ls_vendor_packages.count_documents(
            {'purchase_reference': reference}), 1)


if __name__ == '__main__':
    unittest.main()
