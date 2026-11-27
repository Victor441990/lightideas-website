"""Additive Flask routes for verified-email vendor activation."""

import datetime as dt
import hmac
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask import Blueprint, jsonify, make_response, request

from laptopseal_vendor import (
    VendorLicenseError, approve_challenge, challenge_status, cancel_challenge, create_challenge,
    signed_entitlement, refresh_signed_entitlement, utcnow,
)
from laptopseal_vendor_auth import (
    _take_request_slot, create_vendor_session, get_vendor_session,
    normalize_email, request_verification_code, revoke_vendor_session, rotate_csrf,
    valid_csrf, verify_email_code,
)


COOKIE = 'ls_vendor_session'
PREFIX = '/api/laptopseal/vendor'


def _iso(value):
    return value.isoformat() + 'Z' if value else None


def _signing_key():
    raw = os.environ.get('LAPTOPSEAL_VENDOR_SIGNING_PRIVATE_KEY_PEM', '')
    if not raw:
        raise RuntimeError('Vendor signing key is not configured.')
    key = serialization.load_pem_private_key(raw.replace('\\n', '\n').encode(),
                                             password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError('Vendor signing key must be Ed25519.')
    return key


def register_vendor_api(app, get_db, send_brevo_email, *,
                        deliver_code=None, signing_key_loader=None,
                        transaction_runner=None):
    """Register once on the existing Flask app; injection is for mock tests."""
    bp = Blueprint('laptopseal_vendor', __name__, url_prefix=PREFIX)

    def send_code(email, code):
        if deliver_code is not None:
            return deliver_code(email, code)
        if os.environ.get('LAPTOPSEAL_TEST_MODE') == '1':
            if (local_test_request()
                    and os.environ.get('LAPTOPSEAL_VENDOR_TEST_CODE_CAPTURE') == '1'):
                path = Path(__file__).resolve().parent / '.vendor-test' / 'email-code.txt'
                path.parent.mkdir(exist_ok=True)
                path.write_text(email + '\n' + code + '\n', encoding='utf-8')
                os.chmod(path, 0o600)
                return True
            if phone_test_email_request(email):
                return send_brevo_email(
                    email, 'LaptopSeal vendor sign-in code',
                    '<p>Your LaptopSeal sign-in code is <strong>' + code +
                    '</strong>. It expires in 10 minutes.</p>',
                    'Your LaptopSeal sign-in code is ' + code + '. It expires in 10 minutes.')
            return False
        if not os.environ.get('BREVO_API_KEY'):
            return False
        return send_brevo_email(
            email, 'LaptopSeal vendor sign-in code',
            '<p>Your LaptopSeal sign-in code is <strong>' + code +
            '</strong>. It expires in 10 minutes.</p>',
            'Your LaptopSeal sign-in code is ' + code + '. It expires in 10 minutes.')

    def local_test_request():
        return (os.environ.get('LAPTOPSEAL_TEST_MODE') == '1'
                and os.environ.get('MONGO_DB_NAME') == 'laptopseal_test'
                and request.host.split(':')[0] in ('127.0.0.1', 'localhost')
                and request.remote_addr in ('127.0.0.1', '::1'))

    def phone_test_email_request(email):
        host = os.environ.get('LAPTOPSEAL_PHONE_TEST_HOST', '').strip().lower()
        gateway_token = os.environ.get('LAPTOPSEAL_PHONE_TEST_GATEWAY_TOKEN', '')
        supplied = request.headers.get('X-LaptopSeal-Phone-Gateway', '')
        approved_email = normalize_email(os.environ.get('LAPTOPSEAL_PHONE_TEST_EMAIL'))
        return (os.environ.get('LAPTOPSEAL_PHONE_TEST_EMAIL_ENABLED') == '1'
                and os.environ.get('MONGO_DB_NAME') == 'laptopseal_test'
                and re.fullmatch(
                    r'(?:[a-z0-9-]+\.trycloudflare\.com|[a-z0-9-]+\.[a-z0-9-]+\.ts\.net)',
                    host) is not None
                and request.host.lower() == host
                and request.remote_addr in ('127.0.0.1', '::1')
                and request.headers.get('Origin') == 'https://' + host
                and approved_email is not None
                and normalize_email(email) == approved_email
                and len(gateway_token) >= 32
                and hmac.compare_digest(supplied, gateway_token)
                and bool(os.environ.get('BREVO_API_KEY')))

    def same_origin():
        if request.headers.get('Sec-Fetch-Site', '').lower() == 'cross-site':
            return False
        origin = request.headers.get('Origin')
        if not origin:
            return True
        parsed = urlsplit(origin)
        return (parsed.netloc == request.host
                and parsed.scheme in ('https', 'http')
                and (parsed.scheme == 'https' or local_test_request()))

    def json_data():
        return request.get_json(silent=True) if request.is_json else None

    def current_session():
        return get_vendor_session(get_db(), request.cookies.get(COOKIE))

    def require_vendor(csrf=False):
        record = current_session()
        if not record:
            return None, (jsonify({'error': 'Vendor authentication required.'}), 401)
        if csrf and (not same_origin() or not valid_csrf(
                record, request.headers.get('X-LaptopSeal-CSRF'))):
            return None, (jsonify({'error': 'Invalid request token.'}), 403)
        return record, None

    def package_view(package):
        now = utcnow().replace(tzinfo=None)
        remaining = max(0, package['max_devices'] - package['allocated_count'])
        return {'id': package['_id'], 'plan_id': package['plan_id'],
                'amount_kobo': package['amount_kobo'], 'currency': package['currency'],
                'max_devices': package['max_devices'],
                'allocated_count': package['allocated_count'],
                'remaining_slots': remaining,
                'issued_at': _iso(package['issued_at']),
                'expires_at': _iso(package['expires_at']),
                'status': ('expired' if package['expires_at'] <= now
                           else package['status'])}

    @bp.after_request
    def no_store(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Pragma'] = 'no-cache'
        return response

    @bp.post('/auth/request')
    def request_code():
        data = json_data()
        if data is None or not same_origin():
            return jsonify({'error': 'Invalid request.'}), 400
        # Deliberately identical for known, unknown, throttled, and delivery
        # failure cases. A code is never included in an HTTP response.
        try:
            request_verification_code(get_db(), data.get('email'),
                                      request.remote_addr or 'unknown', send_code)
        except RuntimeError:
            return jsonify({'error': 'Vendor authentication is unavailable.'}), 503
        return jsonify({'message': 'If the address can receive mail, a sign-in code was sent.'}), 202

    @bp.post('/auth/verify')
    def verify_code():
        data = json_data()
        if data is None or not same_origin():
            return jsonify({'error': 'Invalid request.'}), 400
        try:
            vendor = verify_email_code(get_db(), data.get('email'), data.get('code'))
        except RuntimeError:
            return jsonify({'error': 'Vendor authentication is unavailable.'}), 503
        if not vendor:
            return jsonify({'error': 'Code is invalid or expired.'}), 403
        token, csrf = create_vendor_session(
            get_db(), vendor['_id'], request.cookies.get(COOKIE))
        response = make_response(jsonify({'authenticated': True, 'csrf_token': csrf}))
        response.set_cookie(COOKIE, token, max_age=8 * 3600, httponly=True,
                            secure=not local_test_request(),
                            samesite='Lax', path=PREFIX)
        return response

    @bp.get('/session')
    def session_status():
        record, error = require_vendor()
        if error:
            return error
        csrf = rotate_csrf(get_db(), request.cookies.get(COOKIE))
        return jsonify({'authenticated': True, 'csrf_token': csrf,
                        'expires_at': _iso(record['expires_at'])})

    @bp.post('/auth/logout')
    def logout():
        record, error = require_vendor(csrf=True)
        if error:
            return error
        revoke_vendor_session(get_db(), request.cookies.get(COOKIE))
        response = make_response(jsonify({'authenticated': False}))
        response.delete_cookie(COOKIE, path=PREFIX)
        return response

    @bp.get('/packages')
    def packages():
        record, error = require_vendor()
        if error:
            return error
        docs = get_db().ls_vendor_packages.find({'vendor_id': record['vendor_id']}).sort('issued_at', -1)
        return jsonify({'packages': [package_view(p) for p in docs]})

    @bp.get('/packages/<package_id>')
    def package_details(package_id):
        record, error = require_vendor()
        if error:
            return error
        db = get_db()
        package = db.ls_vendor_packages.find_one({'_id': package_id,
                                                   'vendor_id': record['vendor_id']})
        if not package:
            return jsonify({'error': 'Package unavailable.'}), 404
        allocations = db.ls_vendor_allocations.find(
            {'package_id': package_id, 'vendor_id': record['vendor_id']}).sort('slot', 1)
        return jsonify({'package': package_view(package),
                        'allocations': [{'slot': a['slot'],
                                         'device_hint': a['device_hash'][:10],
                                         'allocated_at': _iso(a['allocated_at'])}
                                        for a in allocations]})

    @bp.post('/challenges')
    def new_challenge():
        data = json_data()
        if data is None or not same_origin():
            return jsonify({'error': 'Invalid request.'}), 400
        device_id = data.get('hardware_id')
        # Match the existing desktop get_hardware_id() 32-hex fingerprint.
        if not isinstance(device_id, str) or len(device_id) != 32 or any(
                c not in '0123456789abcdef' for c in device_id):
            return jsonify({'error': 'Invalid device identity.'}), 400
        now = utcnow().replace(tzinfo=None)
        db = get_db()
        try:
            if not _take_request_slot(db, 'challenge-ip:' + (request.remote_addr or 'unknown'),
                                      30, dt.timedelta(seconds=5), now):
                return jsonify({'error': 'Try again later.'}), 429
            if not _take_request_slot(db, 'challenge-device:' + device_id,
                                      12, dt.timedelta(seconds=5), now):
                return jsonify({'error': 'Try again later.'}), 429
        except RuntimeError:
            return jsonify({'error': 'Vendor activation is unavailable.'}), 503
        challenge = create_challenge(db, device_id, now=now)
        return jsonify({'challenge_id': challenge['challenge_id'],
                        'qr': {'challenge_id': challenge['challenge_id'],
                               'qr_secret': challenge['qr_secret']},
                        'poll_secret': challenge['poll_secret'],
                        'expires_at': _iso(challenge['expires_at'])}), 201

    @bp.post('/challenges/<challenge_id>/inspect')
    def inspect_challenge(challenge_id):
        record, error = require_vendor(csrf=True)
        if error:
            return error
        data = json_data() or {}
        secret = data.get('qr_secret')
        if not isinstance(secret, str) or len(secret) < 32:
            return jsonify({'error': 'Challenge unavailable.'}), 404
        from laptopseal_vendor import _digest
        challenge = get_db().ls_vendor_challenges.find_one(
            {'_id': challenge_id, 'qr_hash': _digest(secret), 'status': 'pending',
             'expires_at': {'$gt': utcnow().replace(tzinfo=None)}})
        if not challenge:
            return jsonify({'error': 'Challenge unavailable.'}), 404
        return jsonify({'challenge_id': challenge_id,
                        'device_hint': challenge['device_hash'][:10],
                        'expires_at': _iso(challenge['expires_at'])})

    @bp.post('/challenges/<challenge_id>/approve')
    def approve(challenge_id):
        record, error = require_vendor(csrf=True)
        if error:
            return error
        data = json_data() or {}
        try:
            allocation = approve_challenge(
                get_db(), challenge_id, data.get('qr_secret'), record['vendor_id'],
                data.get('package_id'), transaction_runner=transaction_runner)
        except VendorLicenseError:
            return jsonify({'error': 'Challenge or package unavailable.'}), 409
        except Exception:
            # A database without transaction support must fail closed.
            return jsonify({'error': 'Approval is temporarily unavailable.'}), 503
        return jsonify({'approved': True, 'slot': allocation['slot']})

    @bp.post('/challenges/<challenge_id>/poll')
    def poll(challenge_id):
        data = json_data()
        if data is None or not same_origin():
            return jsonify({'error': 'Invalid request.'}), 400
        secret = data.get('poll_secret')
        try:
            status = challenge_status(get_db(), challenge_id, secret)
            if status['status'] != 'approved':
                return jsonify({'status': status['status']})
            loader = signing_key_loader or _signing_key
            proof = signed_entitlement(get_db(), challenge_id, secret, loader())
            return jsonify({'status': 'approved', 'entitlement': proof})
        except VendorLicenseError:
            return jsonify({'error': 'Challenge unavailable.'}), 404
        except (RuntimeError, ValueError):
            return jsonify({'error': 'Entitlement signing is unavailable.'}), 503

    @bp.post('/challenges/<challenge_id>/cancel')
    def cancel(challenge_id):
        data = json_data()
        if data is None or not same_origin():
            return jsonify({'error': 'Invalid request.'}), 400
        try:
            cancelled = cancel_challenge(get_db(), challenge_id, data.get('poll_secret'))
        except VendorLicenseError:
            return jsonify({'error': 'Challenge unavailable.'}), 404
        if not cancelled:
            return jsonify({'error': 'Challenge is no longer pending.'}), 409
        return jsonify({'status': 'cancelled'})

    @bp.post('/entitlements/check')
    def check_entitlement():
        if not same_origin() or (request.content_length or 0) > 8192:
            return jsonify({'error': 'Invalid request.'}), 400
        data = json_data() or {}
        try:
            loader = signing_key_loader or _signing_key
            renewed = refresh_signed_entitlement(
                get_db(), data.get('entitlement'), data.get('hardware_id'), loader())
            return jsonify({'valid': True, 'entitlement': renewed})
        except VendorLicenseError:
            return jsonify({'valid': False}), 403
        except (RuntimeError, ValueError, TypeError):
            return jsonify({'error': 'Entitlement verification is unavailable.'}), 503

    app.register_blueprint(bp)
    return bp
