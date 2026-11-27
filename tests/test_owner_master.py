"""Dependency-free route tests with a synthetic MongoDB, never production data."""
import ast
import copy
import datetime
import hmac
from pathlib import Path
from types import SimpleNamespace
import time
import unittest

from owner_master_security import (
    OWNER_ACCESS_DAYS, device_token_hash, hash_master_key, new_device_token,
    opaque_id, valid_master_key, verify_master_key,
)
from laptopseal_security import PLANS, token_hash
from werkzeug.security import generate_password_hash, check_password_hash


class FakeCollection:
    def __init__(self):
        self.docs = {}

    def matches(self, doc, query):
        for key, value in query.items():
            if key == '$or':
                if not any(self.matches(doc, option) for option in value):
                    return False
            elif isinstance(value, dict) and '$exists' in value:
                if (key in doc) != value['$exists']:
                    return False
            elif doc.get(key) != value:
                return False
        return True

    def find_one(self, query, projection=None):
        return next((copy.deepcopy(d) for d in self.docs.values() if self.matches(d, query)), None)

    def update_one(self, query, update, upsert=False):
        current = next((d for d in self.docs.values() if self.matches(d, query)), None)
        if current is None and not upsert:
            return SimpleNamespace(modified_count=0)
        if current is None:
            current = {'_id': query['_id']}
            self.docs[current['_id']] = current
            current.update(update.get('$setOnInsert', {}))
        current.update(update.get('$set', {}))
        for key, value in update.get('$inc', {}).items():
            current[key] = current.get(key, 0) + value
        return SimpleNamespace(modified_count=1)

    def find_one_and_update(self, query, update, upsert=False, return_document=None):
        self.update_one(query, update, upsert=upsert)
        return self.find_one(query)


def load_routes(namespace):
    source = ast.parse(Path(__file__).resolve().parents[1].joinpath('app.py').read_text(encoding='utf-8-sig'))
    wanted = {
        '_license_hash', '_find_laptopseal_license', '_owner_admin_authenticated',
        'owner_master_key_admin', 'revoke_owner_master_key', '_owner_attempt_allowed',
        '_record_owner_failed_attempt',
        'laptopseal_owner_activate', 'laptopseal_owner_check',
        'laptopseal_bind_hardware', 'laptopseal_check_license',
    }
    functions = []
    for node in source.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            node = copy.deepcopy(node)
            node.decorator_list = []
            functions.append(node)
    assert len(functions) == len(wanted), 'A required backend route is missing'
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    exec(compile(module, 'app.py', 'exec'), namespace)


class OwnerMasterTests(unittest.TestCase):
    def setUp(self):
        self.collections = {name: FakeCollection() for name in (
            'owner_key', 'owner_devices', 'attempts', 'ls_licenses')}
        self.ns = {
            'datetime': datetime, 'time': time, 'hmac': hmac, 'PLANS': PLANS,
            'ADMIN_PASSWORD_HASH': generate_password_hash('synthetic-admin-password'),
            'check_password_hash': check_password_hash,
            'LAPTOPSEAL_LICENSE_PEPPER': 'synthetic-pepper',
            'OWNER_ACCESS_DAYS': OWNER_ACCESS_DAYS,
            'OWNER_RATE_WINDOW_SECONDS': 900, 'OWNER_RATE_MAX_ATTEMPTS': 5,
            'hash_master_key': hash_master_key, 'verify_master_key': verify_master_key,
            'valid_master_key': valid_master_key, 'device_token_hash': device_token_hash,
            'new_device_token': new_device_token, 'opaque_id': opaque_id,
            'token_hash': token_hash, 'jsonify': lambda value: value,
            'ReturnDocument': SimpleNamespace(AFTER=True),
            'session': {'admin_logged_in': True},
            'get_ls_owner_key_col': lambda: self.collections['owner_key'],
            'get_ls_owner_activations_col': lambda: self.collections['owner_devices'],
            'get_ls_owner_attempts_col': lambda: self.collections['attempts'],
            'get_db': lambda: self.collections,
        }
        load_routes(self.ns)

    def request(self, method='POST', remote_addr='192.0.2.1', **body):
        self.ns['request'] = SimpleNamespace(method=method, remote_addr=remote_addr,
                                             get_json=lambda silent=True: body)

    def set_key(self, value='Synthetic owner phrase, stable and private'):
        self.request(master_key=value, admin_password='synthetic-admin-password')
        result = self.ns['owner_master_key_admin']()
        self.assertTrue(result['success'])
        stored = self.collections['owner_key'].docs['primary']
        self.assertNotIn(value, str(stored))
        return value

    def activate(self, key, hardware_id, remote_addr='192.0.2.1'):
        self.request(master_key=key, hardware_id=hardware_id, remote_addr=remote_addr)
        return self.ns['laptopseal_owner_activate']()

    def check_owner(self, token, hardware_id):
        self.request(owner_token=token, hardware_id=hardware_id)
        return self.ns['laptopseal_owner_check']()

    def test_password_hash_and_admin_protection(self):
        key = self.set_key()
        self.assertTrue(verify_master_key(key, self.collections['owner_key'].docs['primary']['password_hash']))
        original_version = self.collections['owner_key'].docs['primary']['version']
        self.set_key(key)
        self.assertEqual(self.collections['owner_key'].docs['primary']['version'], original_version)
        self.ns['session'].clear()
        self.request(master_key='A different long private phrase', admin_password='synthetic-admin-password')
        self.assertEqual(self.ns['owner_master_key_admin']()[1], 401)

    def test_same_key_multiple_laptops_and_independent_renewal(self):
        key = self.set_key()
        first = self.activate(key, 'laptop-one')
        second = self.activate(key, 'laptop-two')
        self.assertTrue(first['success'] and second['success'])
        self.assertNotEqual(first['owner_token'], second['owner_token'])
        devices = self.collections['owner_devices'].docs
        self.assertEqual(len(devices), 2)
        for device in devices.values():
            self.assertEqual(device['expires_at'] - device['activated_at'],
                             datetime.timedelta(days=30))
        self.assertTrue(self.check_owner(first['owner_token'], 'laptop-one')['valid'])
        self.assertFalse(self.check_owner(first['owner_token'], 'laptop-two')['valid'])
        first_id = opaque_id('laptop-one', 'synthetic-pepper')
        second_id = opaque_id('laptop-two', 'synthetic-pepper')
        second_expiry = devices[second_id]['expires_at']
        devices[first_id]['expires_at'] = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        self.assertFalse(self.check_owner(first['owner_token'], 'laptop-one')['valid'])
        renewed = self.activate(key, 'laptop-one')
        self.assertTrue(renewed['success'])
        self.assertEqual(renewed['days_left'], 30)
        self.assertTrue(self.check_owner(renewed['owner_token'], 'laptop-one')['valid'])
        self.assertEqual(devices[first_id]['expires_at'] - devices[first_id]['activated_at'],
                         datetime.timedelta(days=30))
        self.assertEqual(devices[second_id]['expires_at'], second_expiry)
        self.assertEqual(len(self.collections['ls_licenses'].docs), 0)

    def test_invalid_key_rate_limit_rotation_and_revoke(self):
        key = self.set_key()
        self.assertEqual(self.activate('invalid private owner phrase', 'machine-a')[1], 403)
        for _ in range(4):
            self.activate('invalid private owner phrase', 'machine-a')
        self.assertEqual(self.activate(key, 'machine-a')[1], 429)
        self.assertEqual(self.activate(key, 'machine-a', '192.0.2.2')[1], 429)
        self.request(master_key=key, hardware_id='machine-b', remote_addr='192.0.2.2')
        active = self.ns['laptopseal_owner_activate']()
        self.assertTrue(active['success'])
        replacement = 'Another private owner phrase for rotation'
        self.set_key(replacement)
        self.assertFalse(self.check_owner(active['owner_token'], 'machine-b')['valid'])
        self.assertEqual(self.activate(key, 'machine-c', '192.0.2.3')[1], 403)
        new_session = self.activate(replacement, 'machine-c', '192.0.2.3')
        self.assertTrue(new_session['success'])
        self.request(admin_password='synthetic-admin-password')
        self.assertTrue(self.ns['revoke_owner_master_key']()['success'])
        self.assertFalse(self.check_owner(new_session['owner_token'], 'machine-c')['valid'])

    def test_customer_device_limit_and_expiry(self):
        token = 'synthetic-customer-token'
        self.collections['ls_licenses'].docs[1] = {
            '_id': 1, 'token': token, 'active': True, 'tier': 'single',
            'email': 'buyer@example.test', 'max_devices': 1, 'devices': [],
            'expires_at': datetime.datetime.utcnow() + datetime.timedelta(days=2)}
        self.request(token=token, hardware_id='first-device')
        self.assertTrue(self.ns['laptopseal_bind_hardware']()['success'])
        self.request(token=token, hardware_id='second-device')
        self.assertFalse(self.ns['laptopseal_bind_hardware']()['success'])
        self.assertFalse(self.ns['laptopseal_check_license']()['valid'])
        self.request(token=token, hardware_id='')
        self.assertFalse(self.ns['laptopseal_check_license']()['valid'])
        self.collections['ls_licenses'].docs[1]['expires_at'] = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        self.request(token=token, hardware_id='first-device')
        self.assertFalse(self.ns['laptopseal_check_license']()['valid'])
        self.assertFalse(self.ns['laptopseal_bind_hardware']()['success'])


if __name__ == '__main__':
    unittest.main()
