"""Temporary, loopback-only allowlist gateway for the owner-approved phone test.

This is deliberately separate from the website. An unset hostname denies every
request, and the gateway never serves files or forwards payment/admin routes.
"""

import http.client
import json
import os
import re
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


BACKEND_HOST = '127.0.0.1'
BACKEND_PORT = 5050
GATEWAY_PORT = 5051
PUBLIC_HOST_RE = re.compile(
    r'(?:[a-z0-9-]+\.trycloudflare\.com|[a-z0-9-]+\.[a-z0-9-]+\.ts\.net)\Z')
CHALLENGE_PATH_RE = re.compile(
    r'/api/laptopseal/vendor/challenges/[a-f0-9]{32}/(?:inspect|approve)\Z')
GET_PATHS = frozenset({
    '/laptopseal/vendor',
    '/static/css/laptopseal-vendor.css',
    '/static/js/laptopseal-vendor.js',
    '/static/js/jsQR-1.4.0.js',
    '/api/laptopseal/vendor/session',
    '/api/laptopseal/vendor/packages',
})
POST_PATHS = frozenset({
    '/api/laptopseal/vendor/auth/request',
    '/api/laptopseal/vendor/auth/verify',
    '/api/laptopseal/vendor/auth/logout',
})


def configured_host():
    host = os.environ.get('LAPTOPSEAL_PHONE_TEST_HOST', '').strip().lower()
    return host if PUBLIC_HOST_RE.fullmatch(host) else None


def allowed_route(method, raw_path):
    if (len(raw_path) > 200 or '%' in raw_path or '\\' in raw_path
            or '//' in raw_path or '?' in raw_path or '#' in raw_path):
        return False
    if method == 'GET':
        return raw_path in GET_PATHS
    if method == 'POST':
        return raw_path in POST_PATHS or bool(CHALLENGE_PATH_RE.fullmatch(raw_path))
    return False


class PhoneGateway(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        # Request bodies, cookies, codes, and QR details must never enter logs.
        pass

    def _reject(self, status):
        self.send_response(status)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.send_header('Connection', 'close')
        self.end_headers()

    def do_GET(self):
        self._forward('GET')

    def do_POST(self):
        self._forward('POST')

    def do_HEAD(self):
        self._reject(405)

    def do_OPTIONS(self):
        self._reject(405)

    def _forward(self, method):
        host = configured_host()
        if not host or self.headers.get('Host', '').lower() != host:
            self._reject(421)
            return
        if not allowed_route(method, self.path):
            self._reject(404)
            return
        if self.headers.get('Sec-Fetch-Site', '').lower() == 'cross-site':
            self._reject(403)
            return
        body = b''
        if method == 'POST':
            if self.headers.get('Origin') != 'https://' + host:
                self._reject(403)
                return
            if 'application/json' not in self.headers.get('Content-Type', '').lower():
                self._reject(415)
                return
            if self.headers.get('Transfer-Encoding'):
                self._reject(400)
                return
            length = self.headers.get('Content-Length', '')
            if not length.isdecimal() or int(length) > 4096:
                self._reject(413)
                return
            body = self.rfile.read(int(length))
            if self.path in ('/api/laptopseal/vendor/auth/request',
                             '/api/laptopseal/vendor/auth/verify'):
                try:
                    email = json.loads(body).get('email', '').strip().lower()
                except (ValueError, AttributeError, TypeError):
                    self._reject(400)
                    return
                if email != os.environ.get('LAPTOPSEAL_PHONE_TEST_EMAIL', '').strip().lower():
                    self._reject(403)
                    return
        headers = {'Host': host, 'Accept': 'application/json',
                   'Sec-Fetch-Site': 'same-origin'}
        if method == 'POST':
            headers['Origin'] = 'https://' + host
            headers['Content-Type'] = 'application/json'
            headers['X-LaptopSeal-Phone-Gateway'] = os.environ.get(
                'LAPTOPSEAL_PHONE_TEST_GATEWAY_TOKEN', '')
        csrf = self.headers.get('X-LaptopSeal-CSRF')
        if csrf and len(csrf) <= 256:
            headers['X-LaptopSeal-CSRF'] = csrf
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie', ''))
        except Exception:
            cookies = SimpleCookie()
        if 'ls_vendor_session' in cookies:
            headers['Cookie'] = 'ls_vendor_session=' + cookies['ls_vendor_session'].value
        try:
            connection = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=10)
            connection.request(method, self.path, body=body, headers=headers)
            response = connection.getresponse()
            content = response.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024:
                self._reject(502)
                return
            set_cookie = response.getheader('Set-Cookie')
            if set_cookie and (not set_cookie.startswith('ls_vendor_session=')
                               or ('max-age=0' not in set_cookie.lower()
                                   and ('secure' not in set_cookie.lower()
                                        or 'httponly' not in set_cookie.lower()))):
                self._reject(502)
                return
            self.send_response(response.status)
            self.send_header('Content-Type', response.getheader('Content-Type',
                                                                'application/octet-stream'))
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Permissions-Policy', 'camera=(self), microphone=()')
            self.send_header('Content-Security-Policy',
                             "default-src 'none'; script-src 'self'; style-src 'self'; "
                             "img-src 'self' data: blob:; connect-src 'self'; "
                             "media-src 'self' blob:; object-src 'none'; "
                             "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            if set_cookie:
                self.send_header('Set-Cookie', set_cookie)
            self.end_headers()
            self.wfile.write(content)
        except (OSError, http.client.HTTPException):
            self._reject(502)
        finally:
            if 'connection' in locals():
                connection.close()


def main():
    if (os.environ.get('LAPTOPSEAL_TEST_MODE') != '1'
            or os.environ.get('MONGO_DB_NAME') != 'laptopseal_test'
            or not os.environ.get('PAYSTACK_PUBLIC_KEY', '').startswith('pk_test_')
            or not os.environ.get('PAYSTACK_SECRET_KEY', '').startswith('sk_test_')
            or not os.environ.get('LAPTOPSEAL_PHONE_TEST_EMAIL')
            or not os.environ.get('LAPTOPSEAL_PHONE_TEST_GATEWAY_TOKEN')):
        raise SystemExit('Isolated phone test configuration is incomplete.')
    server = ThreadingHTTPServer((BACKEND_HOST, GATEWAY_PORT), PhoneGateway)
    server.serve_forever(poll_interval=0.2)


if __name__ == '__main__':
    main()
