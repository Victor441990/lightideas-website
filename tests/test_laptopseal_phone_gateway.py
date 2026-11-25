"""The phone gateway is exercised only against a synthetic local upstream."""

import http.client
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import laptopseal_phone_gateway as gateway


HOST = 'owner-test.trycloudflare.com'


class SyntheticBackend(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.reply()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.seen.append((self.path, dict(self.headers), body))
        self.reply()

    def reply(self):
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        if self.path.endswith('/auth/verify'):
            self.send_header('Set-Cookie',
                             'ls_vendor_session=synthetic; Secure; HttpOnly; SameSite=Lax; Path=/api/laptopseal/vendor')
        self.end_headers()
        self.wfile.write(body)


class PhoneGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = ThreadingHTTPServer(('127.0.0.1', 0), SyntheticBackend)
        cls.backend_thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        cls.backend_thread.start()
        cls.backend_port = patch.object(gateway, 'BACKEND_PORT', cls.backend.server_port)
        cls.backend_port.start()
        cls.front = ThreadingHTTPServer(('127.0.0.1', 0), gateway.PhoneGateway)
        cls.front_thread = threading.Thread(target=cls.front.serve_forever, daemon=True)
        cls.front_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.front.shutdown()
        cls.front.server_close()
        cls.backend.shutdown()
        cls.backend.server_close()
        cls.backend_port.stop()

    def setUp(self):
        SyntheticBackend.seen.clear()
        self.env = patch.dict(os.environ, {
            'LAPTOPSEAL_PHONE_TEST_HOST': HOST,
            'LAPTOPSEAL_PHONE_TEST_EMAIL': 'owner@example.test',
            'LAPTOPSEAL_PHONE_TEST_GATEWAY_TOKEN': 'g' * 43,
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def request(self, method, path, *, host=HOST, origin=None, data=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.front.server_port, timeout=3)
        headers = {'Host': host}
        if origin:
            headers['Origin'] = origin
        if data is not None:
            headers['Content-Type'] = 'application/json'
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        connection.close()
        return result

    def test_fails_closed_without_a_configured_host(self):
        with patch.dict(os.environ, {'LAPTOPSEAL_PHONE_TEST_HOST': ''}):
            self.assertEqual(self.request('GET', '/laptopseal/vendor')[0], 421)
        self.assertFalse(SyntheticBackend.seen)

    def test_exact_tailscale_hostname_preserves_gateway_checks(self):
        host = 'desktop-test.tail1234.ts.net'
        with patch.dict(os.environ, {'LAPTOPSEAL_PHONE_TEST_HOST': host}):
            self.assertEqual(self.request('GET', '/laptopseal/vendor', host=host)[0], 200)
            self.assertEqual(self.request('GET', '/laptopseal/vendor', host=HOST)[0], 421)
            self.assertEqual(self.request('GET', '/victor-admin', host=host)[0], 404)
            payload = json.dumps({'email': 'owner@example.test'}).encode()
            self.assertEqual(self.request('POST', '/api/laptopseal/vendor/auth/request',
                                          host=host, origin='https://' + host,
                                          data=payload)[0], 200)
            self.assertEqual(self.request('POST', '/api/laptopseal/vendor/auth/request',
                                          host=host, origin='https://' + HOST,
                                          data=payload)[0], 403)

    def test_admin_payment_internal_and_wrong_host_are_blocked(self):
        for path in ('/victor-admin', '/laptopseal/vendor/purchase',
                     '/api/laptopseal/vendor/challenges', '/api/laptopseal/vendor/entitlements/check',
                     '/static/../.vendor-test/email-code.txt', '/laptopseal/vendor?debug=1'):
            with self.subTest(path=path):
                self.assertEqual(self.request('GET', path)[0], 404)
        self.assertEqual(self.request('GET', '/laptopseal/vendor', host='wrong.test')[0], 421)
        self.assertFalse(SyntheticBackend.seen)

    def test_only_portal_and_authenticated_phone_actions_forward(self):
        self.assertEqual(self.request('GET', '/laptopseal/vendor')[0], 200)
        payload = json.dumps({'email': 'owner@example.test'}).encode()
        status, _, _ = self.request('POST', '/api/laptopseal/vendor/auth/request',
                                    origin='https://' + HOST, data=payload)
        self.assertEqual(status, 200)
        path, headers, body = SyntheticBackend.seen[-1]
        self.assertEqual(path, '/api/laptopseal/vendor/auth/request')
        self.assertEqual(headers['X-LaptopSeal-Phone-Gateway'], 'g' * 43)
        self.assertEqual(headers['Host'], HOST)
        self.assertEqual(body, payload)
        self.assertEqual(self.request('POST', '/api/laptopseal/vendor/auth/request',
                                      origin='https://wrong.test', data=payload)[0], 403)
        self.assertEqual(self.request('POST', '/api/laptopseal/vendor/auth/request',
                                      origin='https://' + HOST,
                                      data=b'{"email":"other@example.test"}')[0], 403)
        self.assertEqual(self.request('POST', '/api/laptopseal/vendor/challenges',
                                      origin='https://' + HOST, data=b'{}')[0], 404)
        approval_path = '/api/laptopseal/vendor/challenges/' + 'a' * 32 + '/approve'
        self.assertEqual(self.request('POST', approval_path,
                                      origin='https://' + HOST, data=b'{}')[0], 200)

    def test_secure_vendor_cookie_passes_through(self):
        data = b'{"email":"owner@example.test","code":"00000000"}'
        status, headers, _ = self.request('POST', '/api/laptopseal/vendor/auth/verify',
                                          origin='https://' + HOST, data=data)
        self.assertEqual(status, 200)
        self.assertIn('Secure', headers['Set-Cookie'])


if __name__ == '__main__':
    unittest.main()
