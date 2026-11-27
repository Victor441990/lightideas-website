"""Verified Paystack purchase to one existing vendor package.

The caller obtains the transaction from Paystack's server-side verification
API. A unique purchase reference on ls_vendor_packages makes retries safe.
"""

import datetime as dt
import uuid

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from laptopseal_security import safe_reference
from laptopseal_vendor import VENDOR_PLANS, VendorLicenseError, _stored, utcnow
from laptopseal_vendor_auth import normalize_email


PRODUCT_ID = 'laptopseal_vendor'
PACKAGE_DAYS = 30


def _paid_at(value):
    if not isinstance(value, str):
        raise VendorLicenseError('Verified payment time is unavailable.')
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise VendorLicenseError('Verified payment time is unavailable.') from exc
    if parsed.tzinfo is None:
        raise VendorLicenseError('Verified payment time is unavailable.')
    # MongoDB persists datetimes to milliseconds. Normalize before comparing a
    # retried Paystack verification with an already-issued package.
    stored = _stored(parsed)
    return stored.replace(microsecond=(stored.microsecond // 1000) * 1000)


def verified_vendor_payment(transaction, intent, *, now=None):
    """Match only server-held intent facts against Paystack-verified facts."""
    if not isinstance(transaction, dict) or not isinstance(intent, dict):
        raise VendorLicenseError('Payment details could not be verified.')
    reference = intent.get('reference')
    plan_id = intent.get('plan_id')
    plan = VENDOR_PLANS.get(plan_id)
    email = normalize_email(intent.get('email'))
    customer = transaction.get('customer') or {}
    metadata = transaction.get('metadata') or {}
    if (intent.get('product_id') != PRODUCT_ID
            or intent.get('state') not in ('pending', 'fulfilled')
            or intent.get('environment') not in ('test', 'production')
            or not safe_reference(reference)
            or not plan or not email or not isinstance(customer, dict)
            or not isinstance(metadata, dict)
            or transaction.get('reference') != reference
            or transaction.get('status') != 'success'
            or transaction.get('currency') != 'NGN'
            or type(transaction.get('amount')) is not int
            or transaction['amount'] != plan['amount_kobo']
            or intent.get('amount') != plan['amount_kobo']
            or intent.get('currency') != 'NGN'
            or normalize_email(customer.get('email')) != email
            or metadata.get('product_id') != PRODUCT_ID
            or metadata.get('plan_id') != plan_id):
        raise VendorLicenseError('Payment details could not be verified.')
    paid_at = _paid_at(transaction.get('paid_at') or transaction.get('paidAt'))
    now = _stored(now or utcnow())
    created_at = intent.get('created_at')
    if (not isinstance(created_at, dt.datetime)
            or paid_at < _stored(created_at) - dt.timedelta(minutes=10)
            or paid_at > now + dt.timedelta(minutes=5)):
        raise VendorLicenseError('Verified payment time is invalid.')
    return paid_at


def require_vendor_payment_indexes(db):
    for collection, name in ((db.ls_payment_intents, 'ls_unique_payment_intent_reference'),
                             (db.ls_vendor_identities, 'vendor_email_unique'),
                             (db.ls_vendor_packages, 'vendor_purchase_unique')):
        if not collection.index_information().get(name, {}).get('unique'):
            raise RuntimeError('Vendor payment indexes are not ready.')


def issue_paid_vendor_package(db, intent, transaction, *, now=None):
    """Return (package, created); a retry repairs intent state without reissuing."""
    require_vendor_payment_indexes(db)
    paid_at = verified_vendor_payment(transaction, intent, now=now)
    if ((intent['environment'] == 'test' and db.name != 'laptopseal_test')
            or (intent['environment'] == 'production' and db.name != 'lightideas')):
        raise VendorLicenseError('Payment environment is unavailable.')
    reference = intent['reference']
    email = normalize_email(intent['email'])
    plan_id = intent['plan_id']
    plan = VENDOR_PLANS[plan_id]
    if db.ls_licenses.find_one({'reference': reference}, {'_id': 1}):
        raise VendorLicenseError('Payment reference is already in use.')

    try:
        vendor = db.ls_vendor_identities.find_one_and_update(
            {'email': email}, {'$setOnInsert': {'created_at': _stored(now or utcnow())}},
            upsert=True, return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:
        vendor = db.ls_vendor_identities.find_one({'email': email})
    if not vendor:
        raise RuntimeError('Vendor identity is unavailable.')
    existing = db.ls_vendor_packages.find_one({'purchase_reference': reference})
    created = False
    if existing is None:
        package = {
            '_id': uuid.uuid4().hex, 'vendor_id': vendor['_id'],
            'plan_id': plan_id, 'amount_kobo': plan['amount_kobo'],
            'currency': 'NGN', 'max_devices': plan['max_devices'],
            'allocated_count': 0, 'issued_at': paid_at,
            'expires_at': paid_at + dt.timedelta(days=PACKAGE_DAYS),
            'purchase_reference': reference, 'source': 'paystack_verified',
            'environment': intent['environment'], 'status': 'active',
            'delivery_status': 'pending', 'created_at': _stored(now or utcnow()),
        }
        try:
            db.ls_vendor_packages.insert_one(package)
            existing, created = package, True
        except DuplicateKeyError:
            existing = db.ls_vendor_packages.find_one({'purchase_reference': reference})
    if (not existing or existing.get('vendor_id') != vendor['_id']
            or existing.get('plan_id') != plan_id
            or existing.get('amount_kobo') != plan['amount_kobo']
            or existing.get('environment') != intent['environment']
            or existing.get('issued_at') != paid_at):
        raise VendorLicenseError('Payment fulfilment state is inconsistent.')
    changed = db.ls_payment_intents.update_one(
        {'reference': reference, 'product_id': PRODUCT_ID,
         'email': email, 'plan_id': plan_id,
         'state': {'$in': ['pending', 'fulfilled']}},
        {'$set': {'state': 'fulfilled', 'package_id': existing['_id'],
                  'verified_paid_at': paid_at}})
    if not changed.matched_count:
        raise VendorLicenseError('Payment intent is unavailable.')
    return existing, created
