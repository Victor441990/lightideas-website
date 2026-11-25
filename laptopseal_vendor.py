"""Phase-one vendor licensing primitives. No public routes or payment fulfilment.

Callers must obtain a verified vendor identity from a trusted authentication
session. The functions in this module must never be wired to a client-supplied
email or vendor id without that check.
"""

import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import uuid

from cryptography.exceptions import InvalidSignature
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError


VENDOR_PLANS = {
    'single': {'amount_kobo': 200000, 'max_devices': 1},
    'basic': {'amount_kobo': 800000, 'max_devices': 5},
    'business': {'amount_kobo': 3000000, 'max_devices': 20},
    'unlimited': {'amount_kobo': 6000000, 'max_devices': 200},
}
PACKAGE_DAYS = 30
CHALLENGE_MINUTES = 5
_EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
_DEVICE_RE = re.compile(r'^[A-Za-z0-9._:-]{16,256}$')


class VendorLicenseError(ValueError):
    """A safe, non-secret vendor activation failure."""


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def _utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _stored(value):
    # PyMongo stores UTC as naive unless configured otherwise.
    return _utc(value).replace(tzinfo=None)


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def device_digest(device_id):
    if not isinstance(device_id, str) or not _DEVICE_RE.fullmatch(device_id):
        raise VendorLicenseError('Invalid device identity.')
    return _digest(device_id)


def ensure_vendor_indexes(db):
    """Additive migration; invoke deliberately against the selected database."""
    db.ls_vendor_identities.create_index('email', unique=True, name='vendor_email_unique')
    db.ls_vendor_packages.create_index('purchase_reference', unique=True, sparse=True,
                                        name='vendor_purchase_unique')
    db.ls_vendor_packages.create_index([('vendor_id', 1), ('expires_at', 1)],
                                        name='vendor_packages_by_expiry')
    db.ls_vendor_allocations.create_index([('package_id', 1), ('device_hash', 1)],
                                           unique=True, name='vendor_device_per_package_unique')
    db.ls_vendor_allocations.create_index([('package_id', 1), ('slot', 1)],
                                           unique=True, name='vendor_slot_unique')
    db.ls_vendor_challenges.create_index('poll_hash', unique=True, name='vendor_poll_unique')
    db.ls_vendor_challenges.create_index('qr_hash', unique=True, name='vendor_qr_unique')
    db.ls_vendor_challenges.create_index('expires_at', expireAfterSeconds=0,
                                         name='vendor_challenge_expiry')


def upsert_verified_vendor(db, email, verified_at):
    """Internal-only: caller must have proved email ownership independently."""
    email = str(email or '').strip().lower()
    if not _EMAIL_RE.fullmatch(email) or not isinstance(verified_at, dt.datetime):
        raise VendorLicenseError('Verified email is required.')
    if _utc(verified_at) > utcnow() + dt.timedelta(minutes=1):
        raise VendorLicenseError('Invalid verification time.')
    return db.ls_vendor_identities.find_one_and_update(
        {'email': email}, {'$set': {'verified_at': _stored(verified_at)},
                           '$setOnInsert': {'created_at': _stored(utcnow())}},
        upsert=True, return_document=ReturnDocument.AFTER)


def issue_test_package(db, vendor_id, plan_id, *, now=None):
    """Create a synthetic package only in the explicitly isolated test DB."""
    if (os.environ.get('LAPTOPSEAL_TEST_MODE') != '1'
            or db.name != 'laptopseal_test'
            or os.environ.get('MONGO_DB_NAME') != 'laptopseal_test'):
        raise VendorLicenseError('Test package issuance requires the isolated test database.')
    vendor = db.ls_vendor_identities.find_one({'_id': vendor_id})
    if not vendor or not vendor.get('verified_at') or plan_id not in VENDOR_PLANS:
        raise VendorLicenseError('Verified vendor and valid package are required.')
    now = _stored(now or utcnow())
    plan = VENDOR_PLANS[plan_id]
    package = {'_id': uuid.uuid4().hex, 'vendor_id': vendor_id,
               'plan_id': plan_id, 'amount_kobo': plan['amount_kobo'],
               'currency': 'NGN', 'max_devices': plan['max_devices'],
               'allocated_count': 0, 'issued_at': now,
               'expires_at': now + dt.timedelta(days=PACKAGE_DAYS),
               'source': 'isolated_test_only', 'environment': 'test',
               'status': 'active'}
    db.ls_vendor_packages.insert_one(package)
    return package


def create_challenge(db, device_id, *, now=None):
    """Desktop receives poll secret; QR carries a distinct single-use secret."""
    device_hash = device_digest(device_id)
    now = _stored(now or utcnow())
    qr_secret = secrets.token_urlsafe(32)
    poll_secret = secrets.token_urlsafe(32)
    challenge_id = uuid.uuid4().hex
    db.ls_vendor_challenges.insert_one({
        '_id': challenge_id, 'device_hash': device_hash,
        'qr_hash': _digest(qr_secret), 'poll_hash': _digest(poll_secret),
        'created_at': now, 'expires_at': now + dt.timedelta(minutes=CHALLENGE_MINUTES),
        'status': 'pending'})
    return {'challenge_id': challenge_id, 'qr_secret': qr_secret,
            'poll_secret': poll_secret, 'expires_at': now + dt.timedelta(minutes=CHALLENGE_MINUTES)}


def _in_transaction(db, callback, transaction_runner=None):
    if transaction_runner is not None:
        return transaction_runner(callback)
    # Production allocation requires a replica-set transaction. Fail closed on
    # standalone MongoDB or a database client that cannot provide sessions.
    with db.client.start_session() as session:
        return session.with_transaction(lambda s: callback(s))


def approve_challenge(db, challenge_id, qr_secret, verified_vendor_id, package_id,
                      *, now=None, transaction_runner=None):
    """Internal approval. verified_vendor_id must come from authenticated email session."""
    if (not isinstance(challenge_id, str) or not challenge_id
            or not isinstance(qr_secret, str) or not qr_secret
            or verified_vendor_id is None
            or not isinstance(package_id, str) or not package_id):
        raise VendorLicenseError('Invalid approval request.')
    now = _stored(now or utcnow())
    qr_hash = _digest(qr_secret)

    def work(session):
        opts = {'session': session} if session is not None else {}
        vendor = db.ls_vendor_identities.find_one(
            {'_id': verified_vendor_id, 'verified_at': {'$exists': True}}, **opts)
        if not vendor:
            raise VendorLicenseError('Verified vendor session required.')
        challenge = db.ls_vendor_challenges.find_one(
            {'_id': challenge_id, 'qr_hash': qr_hash, 'status': 'pending',
             'expires_at': {'$gt': now}}, **opts)
        if not challenge:
            raise VendorLicenseError('Activation challenge is unavailable.')
        package = db.ls_vendor_packages.find_one(
            {'_id': package_id, 'vendor_id': verified_vendor_id,
             'status': 'active', 'expires_at': {'$gt': now}}, **opts)
        if not package or package.get('environment') not in ('test', 'production'):
            raise VendorLicenseError('Package is unavailable.')
        if package['environment'] == 'test' and (
                os.environ.get('LAPTOPSEAL_TEST_MODE') != '1'
                or db.name != 'laptopseal_test'):
            raise VendorLicenseError('Test package cannot be used in production.')

        existing = db.ls_vendor_allocations.find_one(
            {'package_id': package_id, 'device_hash': challenge['device_hash']}, **opts)
        if existing:
            allocation = existing
        else:
            updated = db.ls_vendor_packages.find_one_and_update(
                {'_id': package_id, 'vendor_id': verified_vendor_id,
                 'status': 'active', 'expires_at': {'$gt': now},
                 'allocated_count': {'$lt': package['max_devices']}},
                {'$inc': {'allocated_count': 1}},
                return_document=ReturnDocument.AFTER, **opts)
            if not updated:
                raise VendorLicenseError('Package device limit reached.')
            allocation = {'_id': uuid.uuid4().hex, 'package_id': package_id,
                          'vendor_id': verified_vendor_id,
                          'device_hash': challenge['device_hash'],
                          'slot': updated['allocated_count'], 'allocated_at': now}
            db.ls_vendor_allocations.insert_one(allocation, **opts)
        changed = db.ls_vendor_challenges.update_one(
            {'_id': challenge_id, 'qr_hash': qr_hash, 'status': 'pending',
             'expires_at': {'$gt': now}},
            {'$set': {'status': 'approved', 'package_id': package_id,
                      'allocation_id': allocation['_id'], 'approved_at': now}}, **opts)
        if changed.modified_count != 1:
            raise VendorLicenseError('Activation challenge is unavailable.')
        return allocation

    try:
        return _in_transaction(db, work, transaction_runner)
    except DuplicateKeyError as exc:
        raise VendorLicenseError('Concurrent approval; retry with a new challenge.') from exc


def challenge_status(db, challenge_id, poll_secret, *, now=None):
    if not isinstance(poll_secret, str) or len(poll_secret) < 32:
        raise VendorLicenseError('Invalid polling credential.')
    now = _stored(now or utcnow())
    challenge = db.ls_vendor_challenges.find_one(
        {'_id': challenge_id, 'poll_hash': _digest(poll_secret)})
    if not challenge:
        raise VendorLicenseError('Activation challenge is unavailable.')
    if challenge['expires_at'] <= now:
        return {'status': 'expired'}
    return {'status': challenge['status'],
            'package_id': challenge.get('package_id'),
            'allocation_id': challenge.get('allocation_id')}


def cancel_challenge(db, challenge_id, poll_secret, *, now=None):
    """Only the desktop's polling credential can cancel a pending challenge."""
    if not isinstance(poll_secret, str) or len(poll_secret) < 32:
        raise VendorLicenseError('Invalid polling credential.')
    now = _stored(now or utcnow())
    result = db.ls_vendor_challenges.update_one(
        {'_id': challenge_id, 'poll_hash': _digest(poll_secret),
         'status': 'pending', 'expires_at': {'$gt': now}},
        {'$set': {'status': 'cancelled', 'cancelled_at': now}})
    return result.modified_count == 1


def signed_entitlement(db, challenge_id, poll_secret, private_key, *, now=None):
    """Return a signed, device-bound proof only to the polling desktop."""
    status = challenge_status(db, challenge_id, poll_secret, now=now)
    if status['status'] != 'approved':
        raise VendorLicenseError('Activation has not been approved.')
    challenge = db.ls_vendor_challenges.find_one({'_id': challenge_id})
    package = db.ls_vendor_packages.find_one({'_id': status['package_id']})
    allocation = db.ls_vendor_allocations.find_one({'_id': status['allocation_id']})
    now = _stored(now or utcnow())
    if (not package or not allocation or package['status'] != 'active'
            or package['expires_at'] <= now
            or allocation['device_hash'] != challenge['device_hash']
            or allocation['package_id'] != package['_id']):
        raise VendorLicenseError('Package entitlement is unavailable.')
    if package['environment'] == 'test' and (
            os.environ.get('LAPTOPSEAL_TEST_MODE') != '1'
            or db.name != 'laptopseal_test'):
        raise VendorLicenseError('Test entitlement cannot be used in production.')
    payload = {'version': 1, 'environment': package['environment'],
               'package_id': package['_id'], 'allocation_id': allocation['_id'],
               'device_hash': allocation['device_hash'],
               'plan_id': package['plan_id'], 'max_devices': package['max_devices'],
               'issued_at': now.isoformat() + 'Z',
               'expires_at': package['expires_at'].isoformat() + 'Z'}
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'payload': payload, 'signature': base64.urlsafe_b64encode(
        private_key.sign(encoded)).decode('ascii')}


def refresh_signed_entitlement(db, proof, device_id, private_key, *, now=None):
    """Recheck a signed allocation online without allocating another slot."""
    now = _stored(now or utcnow())
    environment = 'test' if (os.environ.get('LAPTOPSEAL_TEST_MODE') == '1'
                             and db.name == 'laptopseal_test') else 'production'
    if not verify_entitlement(proof, private_key.public_key(), device_id,
                              environment, now=now):
        raise VendorLicenseError('Entitlement is unavailable.')
    old = proof['payload']
    package = db.ls_vendor_packages.find_one({'_id': old['package_id'],
                                               'environment': environment,
                                               'status': 'active',
                                               'expires_at': {'$gt': now}})
    allocation = db.ls_vendor_allocations.find_one({
        '_id': old['allocation_id'], 'package_id': old['package_id'],
        'device_hash': old['device_hash']})
    if not package or not allocation or allocation['vendor_id'] != package['vendor_id']:
        raise VendorLicenseError('Entitlement is unavailable.')
    payload = dict(old)
    payload['issued_at'] = now.isoformat() + 'Z'
    payload['expires_at'] = package['expires_at'].isoformat() + 'Z'
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'payload': payload, 'signature': base64.urlsafe_b64encode(
        private_key.sign(encoded)).decode('ascii')}


def verify_entitlement(proof, public_key, device_id, environment, *, now=None,
                       last_trusted_at=None):
    """Structure verification for a future desktop client; fail closed on rollback.

    This does not provide a tamper-proof offline clock. The desktop must also
    enforce its bounded-offline policy from the existing licensing subsystem.
    """
    try:
        payload = proof['payload']
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
        signature = base64.urlsafe_b64decode(proof['signature'].encode('ascii'))
        public_key.verify(signature, encoded)
        now = _utc(now or utcnow())
        expiry = dt.datetime.fromisoformat(payload['expires_at'].replace('Z', '+00:00'))
        issued = dt.datetime.fromisoformat(payload['issued_at'].replace('Z', '+00:00'))
        if last_trusted_at is not None and now < _utc(last_trusted_at):
            return False
        return (payload['version'] == 1 and payload['environment'] == environment
                and payload['device_hash'] == device_digest(device_id)
                and issued < expiry and issued <= now < expiry)
    except (KeyError, TypeError, ValueError, InvalidSignature, binascii.Error):
        return False
