# -*- coding: utf-8 -*-
"""Password hashing and HS256 JWT issuance/verification.

Deliberately dependency-free: PBKDF2-HMAC-SHA256 ships with the standard
library, and the JWT implementation is ~40 lines of ``hmac``/``base64``. That
keeps the attack surface (and the image size) small, and means the auth path
can be unit-tested without any crypto package installed.

Note: the ``alg`` header is checked *before* the signature is trusted, and only
``HS256`` is accepted, which closes the classic ``alg: none`` / algorithm
confusion holes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

PBKDF2_ITERATIONS = 200_000
_ALGO = 'HS256'


class TokenError(Exception):
    """Raised for any invalid, expired or malformed token."""


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``."""
    if not password:
        raise ValueError('password must not be empty')
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return f'pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}'


def verify_password(password: str, stored: str) -> bool:
    """Constant-time password check. Never raises on malformed input."""
    try:
        scheme, iterations, salt_hex, hash_hex = (stored or '').split('$')
        if scheme != 'pbkdf2_sha256':
            return False
        digest = hashlib.pbkdf2_hmac(
            'sha256', (password or '').encode('utf-8'), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except Exception:  # noqa: BLE001 - a broken hash is simply "not a match"
        return False


# --------------------------------------------------------------------------- #
# JWT (HS256)
# --------------------------------------------------------------------------- #
def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))


def issue_token(
    *,
    subject: str,
    secret: str,
    issuer: str,
    ttl_minutes: int = 720,
    tenant_id: str = '',
    store_id: str = '',
    roles: list[str] | None = None,
    extra: dict | None = None,
) -> str:
    """Sign a compact HS256 JWT carrying the tenant scope and roles."""
    now = int(time.time())
    payload = {
        'sub': subject,
        'iss': issuer,
        'iat': now,
        'exp': now + int(ttl_minutes * 60),
        'tenant_id': tenant_id,
        'store_id': store_id,
        'roles': list(roles or []),
    }
    if extra:
        payload.update(extra)
    header = {'alg': _ALGO, 'typ': 'JWT'}
    signing_input = '.'.join(
        [
            _b64url_encode(json.dumps(header, separators=(',', ':')).encode()),
            _b64url_encode(json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode()),
        ]
    )
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f'{signing_input}.{_b64url_encode(signature)}'


def decode_token(token: str, secret: str, issuer: str) -> dict:
    """Verify signature, algorithm, issuer and expiry. Raises :class:`TokenError`."""
    if not token or token.count('.') != 2:
        raise TokenError('malformed token')
    header_b64, payload_b64, signature_b64 = token.split('.')

    try:
        header = json.loads(_b64url_decode(header_b64))
    except Exception as exc:  # noqa: BLE001
        raise TokenError('malformed header') from exc

    # Algorithm is pinned before any signature work happens.
    if header.get('alg') != _ALGO:
        raise TokenError('unsupported algorithm')

    expected = hmac.new(
        secret.encode(), f'{header_b64}.{payload_b64}'.encode(), hashlib.sha256
    ).digest()
    try:
        provided = _b64url_decode(signature_b64)
    except Exception as exc:  # noqa: BLE001
        raise TokenError('malformed signature') from exc
    if not hmac.compare_digest(expected, provided):
        raise TokenError('bad signature')

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception as exc:  # noqa: BLE001
        raise TokenError('malformed payload') from exc

    if payload.get('iss') != issuer:
        raise TokenError('bad issuer')
    if int(payload.get('exp', 0)) < int(time.time()):
        raise TokenError('token expired')
    if not payload.get('sub'):
        raise TokenError('token missing sub')
    return payload


def roles_from_claim(value) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset(x.strip() for x in value.split(',') if x.strip())
    if isinstance(value, (list, tuple, set)):
        return frozenset(str(x).strip() for x in value if str(x).strip())
    return frozenset()
