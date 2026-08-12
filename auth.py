from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os

import jwt

JWT_ALGORITHM = "HS256"


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    scheme, iterations, salt_hex, expected_hex = encoded.split("$", 3)
    if scheme != "pbkdf2_sha256":
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
    return hmac.compare_digest(derived.hex(), expected_hex)


def create_access_token(subject: str, secret: str, expires_minutes: int = 30) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": subject, "iat": now, "exp": now + timedelta(minutes=expires_minutes)}
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, secret: str) -> dict:
    return jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
