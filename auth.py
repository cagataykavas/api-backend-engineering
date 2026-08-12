from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt

JWT_ALGORITHM = "HS256"


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_hex, expected_hex = encoded.split("$", 3)
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        bytes.fromhex(salt_hex),
        int(iterations),
    )
    return hmac.compare_digest(derived.hex(), expected_hex)


def create_access_token(
    subject: str,
    secret: str,
    *,
    expires_seconds: int | None = None,
    expires_minutes: int = 30,
) -> str:
    now = datetime.now(timezone.utc)
    lifetime = (
        timedelta(seconds=expires_seconds)
        if expires_seconds is not None
        else timedelta(minutes=expires_minutes)
    )
    payload = {"sub": subject, "iat": now, "exp": now + lifetime}
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, secret: str) -> dict:
    return jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])


def verify_access_token(token: str, secret: str) -> dict:
    try:
        return decode_access_token(token, secret)
    except jwt.PyJWTError as exc:
        raise ValueError("invalid or expired access token") from exc
