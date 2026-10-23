"""
Transaction Processing & Fraud Detection API (runs on Amazon ECS Fargate).

Endpoints
  GET  /health              - load balancer health check
  POST /auth/login          - get a JWT (failed attempts are recorded)
  GET  /accounts/me/thresholds - the fraud thresholds for your bank
  POST /transactions        - evaluate a transaction -> "approved" or "flagged"

Interactive docs are served at /docs (Swagger UI).

Configuration (environment variables)
  STATE_TABLE_NAME  DynamoDB table for account history  (unset = in-memory, local mode)
  FRAUD_QUEUE_URL   SQS queue for flagged events          (unset = log only, local mode)
  JWT_SECRET        signing key for tokens                (injected from Secrets Manager)
  DEMO_PASSWORD     shared password for demo customers
  REQUIRE_AUTH      "true" (default) or "false"
"""

from __future__ import annotations

import hmac
import logging
import os
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

import jwt
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth import create_token, decode_token
from fraud_rules import Transaction, evaluate
from models import (
    LoginRequest,
    LoginResponse,
    ThresholdsResponse,
    TransactionRequest,
    TransactionResponse,
)
from publisher import EventPublisher, LogPublisher, SqsPublisher
from storage import DynamoStateStore, MemoryStateStore, StateStore
from tenants import customer_name, tenant_for_account

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("fraud-api")

app = FastAPI(
    title="Banking Fraud Detection API",
    version="1.0.0",
    description=(
        "Evaluates banking transactions against fraud rules in real time. "
        "Suspicious transactions are flagged and sent to SQS for customer notification.\n\n"
        "**How to try it:** call `POST /auth/login`, copy the `access_token`, click "
        "**Authorize** (top right), paste the token, then call `POST /transactions`."
    ),
)

bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------- wiring
@lru_cache
def get_store() -> StateStore:
    table = os.environ.get("STATE_TABLE_NAME")
    if table:
        logger.info("Using DynamoDB table %s for account state", table)
        return DynamoStateStore(table)
    logger.info("STATE_TABLE_NAME not set - using in-memory store (local mode)")
    return MemoryStateStore()


@lru_cache
def get_publisher() -> EventPublisher:
    queue_url = os.environ.get("FRAUD_QUEUE_URL")
    if queue_url:
        return SqsPublisher(queue_url)
    logger.info("FRAUD_QUEUE_URL not set - flagged events will be logged only (local mode)")
    return LogPublisher()


def auth_required() -> bool:
    return os.environ.get("REQUIRE_AUTH", "true").lower() == "true"


def demo_password() -> str:
    return os.environ.get("DEMO_PASSWORD", "Demo@1234")


def current_claims(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Optional[dict]:
    """Returns the token's claims, or None when auth is switched off."""
    if not auth_required():
        return None
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token. Log in first.")
    try:
        return decode_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired. Log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token.")


def _as_utc(ts: Optional[datetime]) -> datetime:
    if ts is None:
        return datetime.now(timezone.utc)
    if ts.tzinfo is None:  # treat timestamps without a timezone as UTC
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


# ---------------------------------------------------------------- endpoints
@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}


@app.post("/auth/login", response_model=LoginResponse, tags=["auth"])
def login(body: LoginRequest, store: StateStore = Depends(get_store)) -> LoginResponse:
    tenant = tenant_for_account(body.account_id)
    password_ok = hmac.compare_digest(body.password.encode(), demo_password().encode())

    if tenant is None or not password_ok:
        if tenant is not None:
            # Remember the failure - the FAILED_LOGINS fraud rule reads this.
            store.record_failed_login(body.account_id, datetime.now(timezone.utc))
            logger.info("Failed login for %s", body.account_id)
        # Same message either way, so attackers can't tell which accounts exist.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid account ID or password.")

    token, expires_in = create_token(body.account_id, tenant.tenant_id)
    return LoginResponse(
        access_token=token, expires_in=expires_in, account_id=body.account_id, bank=tenant.name
    )


@app.get("/accounts/me/thresholds", response_model=ThresholdsResponse, tags=["accounts"])
def my_thresholds(account_id: Optional[str] = None, claims: Optional[dict] = Depends(current_claims)):
    acct = claims["sub"] if claims else account_id
    tenant = tenant_for_account(acct or "")
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown account.")
    return ThresholdsResponse(
        account_id=acct,
        bank=tenant.name,
        large_amount_threshold=tenant.large_amount_threshold,
        geo_window_minutes=tenant.geo_window_minutes,
        max_failed_logins=tenant.max_failed_logins,
        failed_login_window_minutes=tenant.failed_login_window_minutes,
    )


@app.post("/transactions", response_model=TransactionResponse, tags=["transactions"])
def create_transaction(
    body: TransactionRequest,
    claims: Optional[dict] = Depends(current_claims),
    store: StateStore = Depends(get_store),
    publisher: EventPublisher = Depends(get_publisher),
) -> TransactionResponse:
    # 1. Authorisation: a customer can only transact on their own account.
    if claims and claims.get("sub") != body.account_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only transact on your own account.")

    tenant = tenant_for_account(body.account_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown account or bank.")

    # 2. Evaluate the fraud rules using this bank's thresholds.
    now = datetime.now(timezone.utc)
    txn = Transaction(
        account_id=body.account_id,
        amount=body.amount,
        transaction_type=body.transaction_type.value,
        location=body.location.strip(),
        timestamp=_as_utc(body.timestamp),
    )
    history = store.get_history(body.account_id)
    fired = evaluate(txn, history, tenant, now)

    transaction_id = f"txn_{uuid.uuid4().hex[:16]}"
    status_value = "flagged" if fired else "approved"

    # 3. Flagged? Hand off to SQS so the Lambda can notify the customer and log it.
    if fired:
        event = {
            "event_type": "TRANSACTION_FLAGGED",
            "transaction_id": transaction_id,
            "account_id": txn.account_id,
            "tenant_id": tenant.tenant_id,
            "bank": tenant.name,
            "customer_name": customer_name(txn.account_id),
            "amount": txn.amount,
            "transaction_type": txn.transaction_type,
            "location": txn.location,
            "transaction_timestamp": txn.timestamp.isoformat(),
            "flagged_at": now.isoformat(),
            "risk_score": sum(r.weight for r in fired),
            "rules_triggered": [r.rule for r in fired],
            "reasons": [r.reason for r in fired],
        }
        try:
            publisher.publish(event)
        except Exception:
            # Don't silently lose a fraud alert - tell the caller to retry.
            logger.exception("Could not publish flagged transaction %s", transaction_id)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Transaction was flagged but the alert could not be queued. Please retry.",
            )

    # 4. Remember this transaction for the next geo-velocity check.
    store.record_transaction(txn.account_id, txn.location, txn.timestamp)

    logger.info(
        "transaction=%s account=%s bank=%s status=%s rules=%s",
        transaction_id, txn.account_id, tenant.tenant_id, status_value, [r.rule for r in fired],
    )
    return TransactionResponse(
        transaction_id=transaction_id,
        account_id=txn.account_id,
        bank=tenant.name,
        status=status_value,
        risk_score=sum(r.weight for r in fired),
        rules_triggered=[r.rule for r in fired],
        reasons=[r.reason for r in fired],
        evaluated_at=now,
    )
