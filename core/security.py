"""Security primitives: webhook signature verification and token-at-rest.

Two enterprise habits encoded here:

1. Constant-time comparison (``hmac.compare_digest``) — a normal ``==`` leaks
   timing information and is a classic review finding.
2. Fail closed — a missing/blank signature is a rejection, never "dev mode
   convenience".  Bypassing auth in dev is how it ships to prod.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from cryptography.fernet import Fernet, InvalidToken

from core.config import get_settings

log = logging.getLogger(__name__)

_SIGNATURE_PREFIX = "sha256="


def compute_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return _SIGNATURE_PREFIX + digest


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header:
        return False
    header = header.strip()
    if not header.startswith(_SIGNATURE_PREFIX):
        return False
    if not secret:
        return False
    expected = compute_signature(secret, body)
    return hmac.compare_digest(expected, header)


def _fernet() -> Fernet:
    settings = get_settings()
    if settings.fernet_key:
        return Fernet(settings.fernet_key.encode("utf-8"))
    # Dev-only derivation. Never used outside APP_ENV=dev (checked at boot).
    seed = hashlib.sha256(("dev:" + settings.github_webhook_secret).encode("utf-8")).digest()
    import base64

    return Fernet(base64.urlsafe_b64encode(seed))


def seal_token(token: str) -> str:
    if not token:
        return ""
    return _fernet().encrypt(token.encode("utf-8")).decode("utf-8")


def open_token(sealed: str | None) -> str:
    if not sealed:
        return ""
    try:
        return _fernet().decrypt(sealed.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        log.warning("stored repo token cannot be decrypted (key rotated?)")
        return ""


def verify_api_token(expected: str, provided: str | None) -> bool:
    """Constant-time comparison for the API access token."""
    if not expected:
        return False
    if not provided:
        return False
    return hmac.compare_digest(expected, provided.strip())


def mask(secret: str, keep: int = 4) -> str:
    if not secret:
        return ""
    if len(secret) <= keep:
        return "*" * len(secret)
    return secret[:keep] + "*" * (len(secret) - keep)
