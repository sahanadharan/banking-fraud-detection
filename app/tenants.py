"""
Multi-tenant configuration: each bank gets its own fraud thresholds.

Account IDs start with the bank's code, e.g. MAPLE-1001 or LAKESHORE-2001,
so the API always knows which bank's rules to apply.

In production these settings would live in a database or config service so
a bank's risk team could tune them without a code change.
"""

from __future__ import annotations

import re
from typing import Optional

from fraud_rules import TenantConfig

TENANTS: dict[str, TenantConfig] = {
    "MAPLE": TenantConfig(
        tenant_id="MAPLE",
        name="Maple Bank",
        large_amount_threshold=5000.00,
        geo_window_minutes=60,
        max_failed_logins=3,
        failed_login_window_minutes=30,
    ),
    "LAKESHORE": TenantConfig(
        tenant_id="LAKESHORE",
        name="Lakeshore Credit Union",
        large_amount_threshold=2000.00,  # smaller institution, stricter limit
        geo_window_minutes=120,
        max_failed_logins=2,
        failed_login_window_minutes=30,
    ),
}

# Optional friendly names for a few demo customers (used in alert messages).
DEMO_CUSTOMERS: dict[str, str] = {
    "MAPLE-1001": "Alex Chen",
    "MAPLE-1002": "Priya Nair",
    "LAKESHORE-2001": "Jordan Smith",
}

ACCOUNT_ID_PATTERN = re.compile(r"^([A-Z]+)-(\d{4,10})$")


def tenant_for_account(account_id: str) -> Optional[TenantConfig]:
    """Return the bank an account belongs to, or None if it isn't valid."""
    match = ACCOUNT_ID_PATTERN.match(account_id)
    if not match:
        return None
    return TENANTS.get(match.group(1))


def customer_name(account_id: str) -> str:
    return DEMO_CUSTOMERS.get(account_id, "Valued customer")
