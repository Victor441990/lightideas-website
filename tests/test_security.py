import importlib
import os
import re
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

from werkzeug.security import generate_password_hash


class AdminSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update({
            'FLASK_SECRET_KEY': 'local-test-signing-key',
            'ADMIN_PASSWORD_HASH': generate_password_hash('correct horse battery staple'),
            'MONGO_URI': 'mongodb://localhost:27017/lightideas_security_test',
            'SESSION_COOKIE_SECURE': 'false',
            'TRUST_PROXY_HEADERS': 'false',
            'CLOUDINARY_CLOUD_NAME': 'test-cloud',
            'CLOUDINARY_API_KEY': 'test-key',
            'CLOUDINARY_API_SECRET': 'test-secret',
            'BIZTRACK_SERVICE_URL': 'http://127.0.0.1:3999',
            'BIZTRACK_INTEGRATION_TOKEN': 'test-integration-token',
        })
        sys.modules.pop('app', None)
        cls.module = importlib.import_module('app')
        cls.app = cls.module.app
        cls.app.config.update(TESTING=True)

    def setUp(self):
        self.client = self.app.test_client()

    def csrf_token(self):
        response = self.client.get('/victor-admin')
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        return match.group(1)

    def test_dashboard_requires_authentication(self):
        response = self.client.get('/victor-admin/dashboard')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/victor-admin', response.headers['Location'])

    def test_login_requires_csrf_token(self):
        response = self.client.post('/victor-admin', data={'password': 'anything'})
        self.assertEqual(response.status_code, 400)

    def test_valid_login_creates_hardened_session(self):
        response = self.client.post('/victor-admin', data={
            'password': 'correct horse battery staple',
            'csrf_token': self.csrf_token(),
        })
        self.assertEqual(response.status_code, 302)
        cookie = response.headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)

    def test_invalid_login_is_rejected(self):
        response = self.client.post('/victor-admin', data={
            'password': 'not-the-password',
            'csrf_token': self.csrf_token(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Invalid administrator credentials.', response.data)

    def test_login_rate_limiter_locks_after_repeated_failures(self):
        limiter = self.module.LoginRateLimiter()
        for _ in range(limiter.limit.attempts):
            limiter.register_failure('127.0.0.1')
        self.assertTrue(limiter.is_locked('127.0.0.1'))

    def test_admin_write_requires_csrf_token(self):
        with self.client.session_transaction() as active_session:
            active_session['admin_logged_in'] = True
            active_session['csrf_token'] = 'known-test-token'
        response = self.client.post('/api/products', json={'name': 'test'})
        self.assertEqual(response.status_code, 400)

    def test_logout_requires_post_and_csrf_token(self):
        with self.client.session_transaction() as active_session:
            active_session['admin_logged_in'] = True
            active_session['csrf_token'] = 'known-test-token'
        self.assertEqual(self.client.get('/victor-admin/logout').status_code, 405)
        response = self.client.post('/victor-admin/logout', data={'csrf_token': 'known-test-token'})
        self.assertEqual(response.status_code, 302)

    def test_biztrack_api_requires_administrator_authentication(self):
        response = self.client.get('/api/admin/biztrack/overview')
        self.assertEqual(response.status_code, 401)

    def test_biztrack_overview_and_registration_filters_are_server_side(self):
        with self.client.session_transaction() as active_session:
            active_session['admin_logged_in'] = True
            active_session['csrf_token'] = 'known-test-token'
        with patch('app.biztrack_overview', return_value={'visitors': 10, 'registrations': 2, 'conversionRate': 20, 'willingness': [], 'sources': []}):
            response = self.client.get('/api/admin/biztrack/overview')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['registrations'], 2)
        with patch('app.biztrack_registrations', return_value={'registrations': []}) as registrations:
            response = self.client.get('/api/admin/biztrack/registrations?q=Ada&willingness=Maybe&source=launch')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(registrations.call_args.args[0], {'q': 'Ada', 'willingness': 'Maybe', 'source': 'launch'})

    def test_biztrack_csv_export_requires_csrf_and_returns_download(self):
        with self.client.session_transaction() as active_session:
            active_session['admin_logged_in'] = True
            active_session['csrf_token'] = 'known-test-token'
        self.assertEqual(self.client.post('/api/admin/biztrack/export.csv').status_code, 400)
        with patch('app.biztrack_export_csv', return_value=b'"Email"\r\n"ada@example.test"\r\n'):
            response = self.client.post('/api/admin/biztrack/export.csv?source=launch', headers={'X-CSRF-Token': 'known-test-token'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response.content_type)
        self.assertIn(b'ada@example.test', response.data)

    def test_biztrack_page_stays_inside_dashboard_main_container(self):
        template = Path('templates/admin_dashboard.html').read_text(encoding='utf-8')
        main_start = template.index('<div class="main">')
        main_end = template.index('\n  </div>\n</div>\n\n<!-- MODAL -->', main_start)
        page_start = template.index('id="page-biztrack"')
        self.assertEqual(template.count('id="page-biztrack"'), 1)
        self.assertGreater(page_start, main_start)
        self.assertLess(page_start, main_end)
        self.assertIn('@media (max-width:760px)', template)


if __name__ == '__main__':
    unittest.main()
