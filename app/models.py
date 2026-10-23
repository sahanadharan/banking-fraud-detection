"""Request and response shapes for the API (validated automatically by FastAPI)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class TransactionType(str, Enum):
    withdrawal = "withdrawal"
    deposit = "deposit"
    transfer = "transfer"


class LoginRequest(BaseModel):
    account_id: str = Field(examples=["MAPLE-1001"])
    password: str = Field(examples=["Demo@1234"])


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    account_id: str
    bank: str


class TransactionRequest(BaseModel):
    account_id: str = Field(examples=["MAPLE-1001"])
    amount: float = Field(gt=0, le=10_000_000, examples=[250.00])
    transaction_type: TransactionType
    location: str = Field(min_length=2, max_length=100, examples=["Toronto, ON"])
    timestamp: Optional[datetime] = Field(
        default=None,
        description="ISO 8601. If omitted, the server's current time is used.",
        examples=["2026-10-24T14:30:00Z"],
    )


class TransactionResponse(BaseModel):
    transaction_id: str
    account_id: str
    bank: str
    status: Literal["approved", "flagged"]
    risk_score: int
    rules_triggered: list[str]
    reasons: list[str]
    evaluated_at: datetime


class ThresholdsResponse(BaseModel):
    account_id: str
    bank: str
    large_amount_threshold: float
    geo_window_minutes: int
    max_failed_logins: int
    failed_login_window_minutes: int
