"""Server-side, email-verified vendor sessions for LaptopSeal.

No verification code or session token is returned from the code-request API.
The delivery callable is injectable for isolated tests only.
"""

import datetime as dt
import hashlib
import hmac
import os
import re
import secrets

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from laptopseal_vendor import _stored, utcnow, upsert_verified_vendor


CODE_LIFETIME = dt.timedelta(minutes=10)
SESSION_LIFETIME = dt.timedelta(hours=8)
REQUEST_WINDOW = dt.timedelta(hours=1)
REQUEST_COOLDOWN = dt.timedelta(seconds=60)
MAX_EMAIL_REQUESTS = 5
MAX_IP_REQUESTS = 20
MAX_CODE_ATTEMPTS = 5
_EMAIL = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


def _secret():
    # Domain separation avoids reusing the licensing pepper's token hashes.
    pepper = os.environ.get('LAPTOPSEAL_LICENSE_PEPPER', '')
    if len(pepper) < 16:
        raise RuntimeError('Vendor authentication is not configured.')
    return hmac.new(pepper.encode(), b'laptopseal-vendor-auth-v1', hashlib.sha256).digest()


def _hash(value):
    return hmac.new(_secret(), value.encode(), hashlib.sha256).hexdigest()


def normalize_email(email):
    value = str(email or '').strip().lower()
    return value if len(value) <= 254 and _EMAIL.fullmatch(value) else None


def ensure_vendor_auth_indexes(db):
    db.ls_vendor_email_codes.create_index('expires_at', expireAfterSeconds=0,
                                          name='vendor_email_code_expiry')
    db.ls_vendor_auth_counters.create_index('expires_at', expireAfterSeconds=0,
                                            name='vendor_auth_counter_expiry')
    db.ls_vendor_sessions.create_index('expires_at', expireAfterSeconds=0,
                                       name='vendor_session_expiry')
    db.ls_vendor_sessions.create_index([('vendor_id', 1), ('expires_at', 1)],
                                       name='vendor_sessions_by_vendor')


def _take_request_slot(db, identity, limit, cooldown, now):
    collection = db.ls_vendor_auth_counters
    key = _hash(identity)
    existing = collection.find_one({'_id': key})
    if existing is None:
        try:
            collection.insert_one({'_id': key, 'count': 1,
                                   'next_allowed_at': now + cooldown,
                                   'expires_at': now + REQUEST_WINDOW})
            return True
        except DuplicateKeyError:
            return False
    if existing['expires_at'] <= now:
        result = collection.update_one({'_id': key, 'expires_at': {'$lte': now}},
                                       {'$set': {'count': 1,
                                                 'next_allowed_at': now + cooldown,
                                                 'expires_at': now + REQUEST_WINDOW}})
        return result.modified_count == 1
    result = collection.update_one(
        {'_id': key, 'expires_at': {'$gt': now}, 'count': {'$lt': limit},
         'next_allowed_at': {'$lte': now}},
        {'$inc': {'count': 1}, '$set': {'next_allowed_at': now + cooldown}})
    return result.modified_count == 1


def request_verification_code(db, email, remote_ip, deliver, *, now=None):
    """Return no account-existence information. The caller returns a generic response."""
    now = _stored(now or utcnow())
    email = normalize_email(email)
    if not email or not isinstance(remote_ip, str) or not remote_ip:
        return False
    if not _take_request_slot(db, 'email:' + email, MAX_EMAIL_REQUESTS,
                              REQUEST_COOLDOWN, now):
        return False
    if not _take_request_slot(db, 'ip:' + remote_ip, MAX_IP_REQUESTS,
                              dt.timedelta(0), now):
        return False
    code = f'{secrets.randbelow(100000000):08d}'
    nonce = secrets.token_urlsafe(16)
    db.ls_vendor_email_codes.replace_one(
        {'_id': _hash('email-code:' + email)},
        {'_id': _hash('email-code:' + email), 'email': email,
         'nonce': nonce, 'code_hash': _hash(nonce + ':' + email + ':' + code),
         'attempts': 0, 'created_at': now, 'expires_at': now + CODE_LIFETIME},
        upsert=True)
    try:
        return bool(deliver(email, code))
    except Exception:
        return False


def verify_email_code(db, email, code, *, now=None):
    now = _stored(now or utcnow())
    email = normalize_email(email)
    if not email or not isinstance(code, str) or not re.fullmatch(r'\d{8}', code):
        return None
    key = _hash('email-code:' + email)
    attempt = db.ls_vendor_email_codes.find_one_and_update(
        {'_id': key, 'expires_at': {'$gt': now},
         'used_at': {'$exists': False}, 'attempts': {'$lt': MAX_CODE_ATTEMPTS}},
        {'$inc': {'attempts': 1}}, return_document=ReturnDocument.AFTER)
    if not attempt:
        return None
    expected = _hash(attempt['nonce'] + ':' + email + ':' + code)
    if not hmac.compare_digest(expected, attempt['code_hash']):
        return None
    claimed = db.ls_vendor_email_codes.update_one(
        {'_id': key, 'code_hash': expected, 'used_at': {'$exists': False},
         'expires_at': {'$gt': now}}, {'$set': {'used_at': now}})
    if claimed.modified_count != 1:
        return None
    return upsert_verified_vendor(db, email, now)


def create_vendor_session(db, vendor_id, previous_token=None, *, now=None):
    now = _stored(now or utcnow())
    if not db.ls_vendor_identities.find_one({'_id': vendor_id,
                                             'verified_at': {'$exists': True}}):
        raise ValueError('Verified vendor required.')
    if previous_token:
        revoke_vendor_session(db, previous_token, now=now)
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    db.ls_vendor_sessions.insert_one({'_id': _hash('session:' + token),
                                      'vendor_id': vendor_id,
                                      'csrf_hash': _hash('csrf:' + csrf),
                                      'created_at': now,
                                      'expires_at': now + SESSION_LIFETIME})
    return token, csrf


def get_vendor_session(db, token, *, now=None):
    if not isinstance(token, str) or len(token) < 32:
        return None
    now = _stored(now or utcnow())
    record = db.ls_vendor_sessions.find_one({'_id': _hash('session:' + token),
                                             'revoked_at': {'$exists': False},
                                             'expires_at': {'$gt': now}})
    if not record:
        return None
    if not db.ls_vendor_identities.find_one({'_id': record['vendor_id'],
                                             'verified_at': {'$exists': True}}):
        return None
    return record


def rotate_csrf(db, token, *, now=None):
    record = get_vendor_session(db, token, now=now)
    if not record:
        return None
    csrf = secrets.token_urlsafe(32)
    db.ls_vendor_sessions.update_one({'_id': record['_id'],
                                      'revoked_at': {'$exists': False}},
                                     {'$set': {'csrf_hash': _hash('csrf:' + csrf)}})
    return csrf


def valid_csrf(record, supplied):
    return (isinstance(supplied, str) and len(supplied) >= 32
            and hmac.compare_digest(record.get('csrf_hash', ''),
                                    _hash('csrf:' + supplied)))


def revoke_vendor_session(db, token, *, now=None):
    if not isinstance(token, str) or len(token) < 32:
        return False
    now = _stored(now or utcnow())
    result = db.ls_vendor_sessions.update_one(
        {'_id': _hash('session:' + token), 'revoked_at': {'$exists': False}},
        {'$set': {'revoked_at': now}})
    return result.modified_count == 1
