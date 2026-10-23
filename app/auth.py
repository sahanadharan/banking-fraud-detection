"""
JWT authentication (bonus challenge).

A customer logs in with POST /auth/login and receives a short-lived token.
Every /transactions call must send it as:  Authorization: Bearer <token>

The signing secret comes from AWS Secrets Manager in production (injected
into the container as the JWT_SECRET environment variable).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import jwt

ALGORITHM = "HS256"
TOKEN_TTL_MINUTES = 60


def _secret() -> str:
    return os.environ.get("JWT_SECRET", "local-dev-only-secret-change-me-in-production")


def create_token(account_id: str, tenant_id: str) -> tuple[str, int]:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": account_id,
        "tenant_id": tenant_id,
        "iat": now,
        "exp": now + timedelta(minutes=TOKEN_TTL_MINUTES),
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM), TOKEN_TTL_MINUTES * 60


def decode_token(token: str) -> dict:
    """Raises jwt.InvalidTokenError (or a subclass) if the token is bad or expired."""
    return jwt.decode(token, _secret(), algorithms=[ALGORITHM])
