"""Small, testable primitives for the owner password and device sessions."""
import hashlib
import hmac
import secrets


MASTER_KEY_MIN_LENGTH = 20
MASTER_KEY_MAX_LENGTH = 256
OWNER_ACCESS_DAYS = 30
OWNER_RATE_WINDOW_SECONDS = 15 * 60
OWNER_RATE_MAX_ATTEMPTS = 5


def valid_master_key(value):
    return (isinstance(value, str)
            and MASTER_KEY_MIN_LENGTH <= len(value) <= MASTER_KEY_MAX_LENGTH
            and value == value.strip())


def hash_master_key(value):
    if not valid_master_key(value):
        raise ValueError('Owner key must be 20–256 characters without leading or trailing spaces.')
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(value.encode('utf-8'), salt=salt, n=16384, r=8, p=1)
    return 'scrypt$16384$8$1$' + salt.hex() + '$' + digest.hex()


def verify_master_key(value, stored_hash):
    if not isinstance(value, str) or not isinstance(stored_hash, str):
        return False
    try:
        scheme, n, r, p, salt, expected = stored_hash.split('$')
        if (scheme, n, r, p) != ('scrypt', '16384', '8', '1'):
            return False
        actual = hashlib.scrypt(value.encode('utf-8'), salt=bytes.fromhex(salt),
                                n=16384, r=8, p=1)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError, MemoryError):
        return False


def device_token_hash(token, pepper):
    if not token or not pepper:
        raise ValueError('Device token and server pepper are required.')
    return hmac.new(pepper.encode('utf-8'), token.encode('utf-8'), hashlib.sha256).hexdigest()


def new_device_token():
    return 'LSOD_' + secrets.token_urlsafe(32)


def opaque_id(value, pepper):
    return device_token_hash(str(value), pepper)
