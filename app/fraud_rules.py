"""
Fraud detection rules.

This module is deliberately "pure": it has no AWS code, no web code and no
database code. It takes a transaction, the account's recent history and the
bank's thresholds, and returns the list of rules that fired. That makes the
business logic easy to read, easy to unit test and easy to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

# Transaction types where money leaves the account. Deposits are excluded
# from the large-amount rule because the risk for a customer is money going out.
OUTGOING_TYPES = {"withdrawal", "transfer"}

# Each rule carries a weight. Any rule firing flags the transaction; the
# total weight is reported as a risk score so analysts can prioritise reviews.
RULE_WEIGHTS = {
    "LARGE_AMOUNT": 40,
    "GEO_VELOCITY": 35,
    "FAILED_LOGINS": 50,
}


@dataclass(frozen=True)
class TenantConfig:
    """Fraud thresholds for one bank (tenant)."""

    tenant_id: str
    name: str
    large_amount_threshold: float  # flag outgoing amounts above this ($)
    geo_window_minutes: int  # two different locations within this window = suspicious
    max_failed_logins: int  # this many failed logins (or more) before a transaction = suspicious
    failed_login_window_minutes: int  # how far back to count failed logins


@dataclass(frozen=True)
class Transaction:
    account_id: str
    amount: float
    transaction_type: str  # withdrawal | deposit | transfer
    location: str
    timestamp: datetime  # timezone-aware


@dataclass
class AccountHistory:
    """What we remember about an account between requests."""

    last_location: Optional[str] = None
    last_timestamp: Optional[datetime] = None
    recent_failed_logins: list[datetime] = field(default_factory=list)


@dataclass(frozen=True)
class RuleResult:
    rule: str
    reason: str
    weight: int


def normalize_location(location: str) -> str:
    """'  Toronto,  ON ' and 'toronto, on' should count as the same place."""
    return " ".join(location.lower().replace(",", " ").split())


# --------------------------------------------------------------------------
# Rule 1: unusually large withdrawal / transfer
# --------------------------------------------------------------------------
def check_large_amount(txn: Transaction, config: TenantConfig) -> Optional[RuleResult]:
    if txn.transaction_type in OUTGOING_TYPES and txn.amount > config.large_amount_threshold:
        return RuleResult(
            rule="LARGE_AMOUNT",
            reason=(
                f"{txn.transaction_type.capitalize()} of ${txn.amount:,.2f} is above "
                f"{config.name}'s limit of ${config.large_amount_threshold:,.2f}"
            ),
            weight=RULE_WEIGHTS["LARGE_AMOUNT"],
        )
    return None


# --------------------------------------------------------------------------
# Rule 2: different locations within a short time window ("impossible travel")
# --------------------------------------------------------------------------
def check_geo_velocity(
    txn: Transaction, history: AccountHistory, config: TenantConfig
) -> Optional[RuleResult]:
    if not history.last_location or not history.last_timestamp:
        return None  # first transaction we've seen for this account

    if normalize_location(txn.location) == normalize_location(history.last_location):
        return None  # same place, nothing unusual

    minutes_apart = abs((txn.timestamp - history.last_timestamp).total_seconds()) / 60
    if minutes_apart <= config.geo_window_minutes:
        return RuleResult(
            rule="GEO_VELOCITY",
            reason=(
                f"Transaction in {txn.location} only {minutes_apart:.0f} minutes after "
                f"a transaction in {history.last_location} "
                f"(window: {config.geo_window_minutes} min)"
            ),
            weight=RULE_WEIGHTS["GEO_VELOCITY"],
        )
    return None


# --------------------------------------------------------------------------
# Rule 3: too many failed logins shortly before the transaction
# --------------------------------------------------------------------------
def check_failed_logins(
    history: AccountHistory, config: TenantConfig, now: datetime
) -> Optional[RuleResult]:
    window_start = now - timedelta(minutes=config.failed_login_window_minutes)
    recent = [t for t in history.recent_failed_logins if window_start <= t <= now]
    if len(recent) >= config.max_failed_logins:
        return RuleResult(
            rule="FAILED_LOGINS",
            reason=(
                f"{len(recent)} failed login attempts in the last "
                f"{config.failed_login_window_minutes} minutes "
                f"(limit: {config.max_failed_logins})"
            ),
            weight=RULE_WEIGHTS["FAILED_LOGINS"],
        )
    return None


def evaluate(
    txn: Transaction, history: AccountHistory, config: TenantConfig, now: datetime
) -> list[RuleResult]:
    """Run every rule and return the ones that fired (empty list = approved)."""
    results = [
        check_large_amount(txn, config),
        check_geo_velocity(txn, history, config),
        check_failed_logins(history, config, now),
    ]
    return [r for r in results if r is not None]
