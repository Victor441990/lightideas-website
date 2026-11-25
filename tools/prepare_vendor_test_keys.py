"""Generate one isolated Ed25519 key pair without printing key material."""

import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / '.env.laptopseal.test'
PUBLIC_PATH = ROOT / '.vendor-test' / 'public.pem'
NAME = 'LAPTOPSEAL_VENDOR_SIGNING_PRIVATE_KEY_PEM'


def main():
    if not ENV_PATH.is_file():
        raise SystemExit('Isolated test configuration is missing.')
    lines = ENV_PATH.read_text(encoding='utf-8').splitlines()
    settings = dict(line.split('=', 1) for line in lines if '=' in line and not line.startswith('#'))
    if settings.get('MONGO_DB_NAME') != 'laptopseal_test':
        raise SystemExit('Refusing signing setup outside laptopseal_test.')
    if settings.get(NAME):
        private = serialization.load_pem_private_key(
            settings[NAME].replace('\\n', '\n').encode('ascii'), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            raise SystemExit('Existing test signing key is not Ed25519.')
    else:
        private = Ed25519PrivateKey.generate()
        encoded = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()).decode('ascii')
        with ENV_PATH.open('a', encoding='utf-8') as handle:
            handle.write('\n' + NAME + '=' + encoded.replace('\n', '\\n') + '\n')
    PUBLIC_PATH.parent.mkdir(exist_ok=True)
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)
    PUBLIC_PATH.write_bytes(public)
    os.chmod(ENV_PATH, 0o600)
    print('Isolated vendor signing key configured; matching public key file ready.')


if __name__ == '__main__':
    main()
