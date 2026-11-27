"""Security primitives for LaptopSeal licensing and Paystack fulfilment.

This module deliberately contains no credentials.  Production supplies the
license pepper and Paystack secret through environment variables.
"""
import hashlib
import hmac
import re
import secrets


LICENSE_TOKEN_BYTES = 32
OWNER_TOKEN_BYTES = 32


PLANS = {
    'single': {'amount': 200000, 'max_devices': 1, 'label': 'Single (1 laptop)'},
    'reseller_5': {'amount': 800000, 'max_devices': 5, 'label': 'Reseller 5 laptops'},
    'reseller_20': {'amount': 3000000, 'max_devices': 20, 'label': 'Reseller 20 laptops'},
    # The public offer is intentionally unlimited.  This sentinel preserves
    # that promise without adding a separate bypass path in activation code.
    'unlimited': {'amount': 6000000, 'max_devices': 999999, 'label': 'Unlimited laptops'},
}


def normalise_email(value):
    return str(value or '').strip().lower()


def make_token(prefix='LS'):
    return prefix + '_' + secrets.token_urlsafe(LICENSE_TOKEN_BYTES)


def token_hash(token, pepper):
    if not token or not pepper:
        raise ValueError('A token and server-side license pepper are required.')
    return hmac.new(pepper.encode('utf-8'), token.encode('utf-8'), hashlib.sha256).hexdigest()


def token_prefix(token):
    return str(token or '')[:12]


def safe_reference(reference):
    return bool(re.fullmatch(r'[A-Za-z0-9_.-]{8,160}', str(reference or '')))


def paystack_signature_is_valid(raw_body, supplied_signature, secret):
    if not raw_body or not supplied_signature or not secret:
        return False
    expected = hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, str(supplied_signature))


def verified_plan_from_transaction(transaction, requested_plan, requested_email):
    """Return a plan id only when Paystack's verified facts match checkout."""
    if not isinstance(transaction, dict) or transaction.get('status') != 'success':
        return None
    if transaction.get('currency') != 'NGN':
        return None
    plan = PLANS.get(requested_plan)
    if not plan or transaction.get('amount') != plan['amount']:
        return None
    customer = transaction.get('customer') or {}
    if normalise_email(customer.get('email')) != normalise_email(requested_email):
        return None
    metadata = transaction.get('metadata') or {}
    is_current_checkout = (
        metadata.get('product_id') == 'laptopseal'
        and metadata.get('plan_id') == requested_plan
    )
    # Existing browser sessions use the old descriptive metadata.  Keep that
    # narrow compatibility path while amount, email, and plan still have to
    # match Paystack's verified transaction.
    legacy_product = str(metadata.get('product') or '')
    is_legacy_checkout = (
        legacy_product.startswith('LaptopSeal')
        and legacy_product.endswith(plan['label'])
    )
    if not (is_current_checkout or is_legacy_checkout):
        return None
    return requested_plan
