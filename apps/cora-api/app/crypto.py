"""Authenticated-encryption utility for credential secrets (v0.6).

Keyed by the env var CORA_CREDENTIAL_ENC_KEY (a Fernet key: url-safe base64 of
32 bytes). If the key is missing or invalid, encryption is UNAVAILABLE and any
attempt to encrypt/decrypt a real secret raises CryptoUnavailable — callers must
fail safely and mark the provider unavailable rather than store plaintext.

HARD RULES: never store plaintext secrets; never log token material. This module
only ever logs whether a key is present/valid — never any value passed to it.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

KEY_ENV = "CORA_CREDENTIAL_ENC_KEY"

try:  # cryptography ships a manylinux wheel; import is cheap.
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # pragma: no cover - dependency missing
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore


class CryptoUnavailable(Exception):
    """Raised when encryption is requested but no valid key is configured."""


def _load_fernet():
    key = os.environ.get(KEY_ENV)
    if not key:
        logger.warning("%s not set — credential encryption is unavailable", KEY_ENV)
        return None
    if Fernet is None:
        logger.error("cryptography library unavailable — encryption disabled")
        return None
    try:
        return Fernet(key.encode("utf-8"))
    except Exception:
        # Do NOT log the key value.
        logger.error("%s is invalid (not a valid Fernet key) — encryption disabled", KEY_ENV)
        return None


_FERNET = _load_fernet()


def encryption_available() -> bool:
    return _FERNET is not None


def encrypt_secret(plaintext: Optional[str]) -> Optional[str]:
    """Encrypt a secret to an opaque token string. Returns None for empty input.
    Raises CryptoUnavailable if no key is configured."""
    if plaintext is None or plaintext == "":
        return None
    if _FERNET is None:
        raise CryptoUnavailable(
            f"encryption unavailable: set {KEY_ENV} to a valid Fernet key"
        )
    return _FERNET.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: Optional[str]) -> Optional[str]:
    """Decrypt a token produced by encrypt_secret. Returns None for empty input.
    Raises CryptoUnavailable if no key is configured or the token is invalid."""
    if token is None or token == "":
        return None
    if _FERNET is None:
        raise CryptoUnavailable(
            f"decryption unavailable: set {KEY_ENV} to a valid Fernet key"
        )
    try:
        return _FERNET.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:  # do not log token material
        raise CryptoUnavailable("stored secret could not be decrypted") from exc
