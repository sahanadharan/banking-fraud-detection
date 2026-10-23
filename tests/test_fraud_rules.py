"""Unit tests for the fraud rules - no AWS or web server needed."""

from datetime import datetime, timedelta, timezone

from fraud_rules import AccountHistory, Transaction, evaluate, normalize_location
from tenants import TENANTS, tenant_for_account

MAPLE = TENANTS["MAPLE"]  # $5,000 limit, 60 min geo window, 3 failed logins
LAKESHORE = TENANTS["LAKESHORE"]  # $2,000 limit, 120 min geo window, 2 failed logins
NOW = datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)


def txn(amount=100.0, ttype="withdrawal", location="Toronto, ON", ts=NOW, account="MAPLE-1001"):
    return Transaction(account, amount, ttype, location, ts)


def rules_fired(transaction, history=None, config=MAPLE):
    return [r.rule for r in evaluate(transaction, history or AccountHistory(), config, NOW)]


# ---- large amount ------------------------------------------------------------
def test_normal_withdrawal_is_approved():
    assert rules_fired(txn(amount=200)) == []


def test_large_withdrawal_is_flagged():
    assert rules_fired(txn(amount=7500)) == ["LARGE_AMOUNT"]


def test_large_transfer_is_flagged():
    assert rules_fired(txn(amount=7500, ttype="transfer")) == ["LARGE_AMOUNT"]


def test_large_deposit_is_not_flagged():
    assert rules_fired(txn(amount=7500, ttype="deposit")) == []


def test_amount_exactly_at_threshold_is_allowed():
    assert rules_fired(txn(amount=5000)) == []


def test_thresholds_differ_per_bank():
    # $3,000 is fine at Maple Bank but over Lakeshore's $2,000 limit
    assert rules_fired(txn(amount=3000)) == []
    assert rules_fired(txn(amount=3000, account="LAKESHORE-2001"), config=LAKESHORE) == ["LARGE_AMOUNT"]


# ---- geo velocity ------------------------------------------------------------
def test_different_city_within_window_is_flagged():
    history = AccountHistory(last_location="Toronto, ON", last_timestamp=NOW - timedelta(minutes=15))
    assert rules_fired(txn(location="Vancouver, BC"), history) == ["GEO_VELOCITY"]


def test_different_city_outside_window_is_ok():
    history = AccountHistory(last_location="Toronto, ON", last_timestamp=NOW - timedelta(hours=5))
    assert rules_fired(txn(location="Vancouver, BC"), history) == []


def test_same_city_written_differently_is_ok():
    history = AccountHistory(last_location="toronto,ON", last_timestamp=NOW - timedelta(minutes=5))
    assert rules_fired(txn(location="  Toronto,  ON "), history) == []


def test_first_transaction_has_no_geo_check():
    assert rules_fired(txn(location="London, UK")) == []


def test_normalize_location():
    assert normalize_location(" Toronto ,ON ") == normalize_location("toronto, on")


# ---- failed logins -----------------------------------------------------------
def test_three_recent_failed_logins_is_flagged():
    history = AccountHistory(recent_failed_logins=[NOW - timedelta(minutes=m) for m in (1, 2, 3)])
    assert rules_fired(txn(), history) == ["FAILED_LOGINS"]


def test_two_failed_logins_ok_at_maple_but_flagged_at_lakeshore():
    history = AccountHistory(recent_failed_logins=[NOW - timedelta(minutes=m) for m in (1, 2)])
    assert rules_fired(txn(), history) == []
    assert rules_fired(txn(account="LAKESHORE-2001"), history, LAKESHORE) == ["FAILED_LOGINS"]


def test_old_failed_logins_are_ignored():
    history = AccountHistory(recent_failed_logins=[NOW - timedelta(hours=h) for h in (2, 3, 4)])
    assert rules_fired(txn(), history) == []


# ---- combined ----------------------------------------------------------------
def test_multiple_rules_add_up_risk_score():
    history = AccountHistory(
        last_location="Toronto, ON",
        last_timestamp=NOW - timedelta(minutes=10),
        recent_failed_logins=[NOW - timedelta(minutes=m) for m in (1, 2, 3)],
    )
    results = evaluate(txn(amount=9000, location="Miami, FL"), history, MAPLE, NOW)
    assert {r.rule for r in results} == {"LARGE_AMOUNT", "GEO_VELOCITY", "FAILED_LOGINS"}
    assert sum(r.weight for r in results) == 125


# ---- tenants -----------------------------------------------------------------
def test_account_id_maps_to_bank():
    assert tenant_for_account("MAPLE-1001").name == "Maple Bank"
    assert tenant_for_account("LAKESHORE-2001").name == "Lakeshore Credit Union"
    assert tenant_for_account("UNKNOWN-1001") is None
    assert tenant_for_account("maple-1001") is None
    assert tenant_for_account("MAPLE-12") is None
