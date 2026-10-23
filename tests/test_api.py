"""End-to-end tests of the API running in local mode (in-memory store, no AWS)."""

import os

os.environ.pop("STATE_TABLE_NAME", None)
os.environ.pop("FRAUD_QUEUE_URL", None)
os.environ["REQUIRE_AUTH"] = "true"
os.environ["DEMO_PASSWORD"] = "Demo@1234"
os.environ["JWT_SECRET"] = "test-secret-that-is-at-least-32-bytes-long"

import pytest
from fastapi.testclient import TestClient

import main
from publisher import LogPublisher
from storage import MemoryStateStore


@pytest.fixture
def ctx():
    store, publisher = MemoryStateStore(), LogPublisher()
    main.app.dependency_overrides[main.get_store] = lambda: store
    main.app.dependency_overrides[main.get_publisher] = lambda: publisher
    yield TestClient(main.app), publisher
    main.app.dependency_overrides.clear()


def login(client, account, password="Demo@1234"):
    return client.post("/auth/login", json={"account_id": account, "password": password})


def headers_for(client, account):
    token = login(client, account).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def send(client, headers, **overrides):
    body = {"account_id": "MAPLE-1001", "amount": 100, "transaction_type": "withdrawal",
            "location": "Toronto, ON"}
    body.update(overrides)
    return client.post("/transactions", json=body, headers=headers)


def test_health(ctx):
    client, _ = ctx
    assert client.get("/health").json() == {"status": "ok"}


def test_transaction_requires_token(ctx):
    client, _ = ctx
    assert send(client, {}).status_code == 401


def test_wrong_password_rejected(ctx):
    client, _ = ctx
    assert login(client, "MAPLE-1001", "wrong").status_code == 401


def test_normal_transaction_is_approved_and_not_queued(ctx):
    client, publisher = ctx
    r = send(client, headers_for(client, "MAPLE-1001"))
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    assert publisher.sent == []


def test_large_withdrawal_is_flagged_and_queued(ctx):
    client, publisher = ctx
    r = send(client, headers_for(client, "MAPLE-1001"), amount=7500)
    body = r.json()
    assert body["status"] == "flagged"
    assert body["rules_triggered"] == ["LARGE_AMOUNT"]
    assert len(publisher.sent) == 1
    assert publisher.sent[0]["transaction_id"] == body["transaction_id"]


def test_geo_velocity_across_two_requests(ctx):
    client, _ = ctx
    h = headers_for(client, "MAPLE-1002")
    first = send(client, h, account_id="MAPLE-1002", location="Toronto, ON",
                 timestamp="2026-10-24T14:00:00Z")
    second = send(client, h, account_id="MAPLE-1002", location="Vancouver, BC",
                  timestamp="2026-10-24T14:20:00Z")
    assert first.json()["status"] == "approved"
    assert second.json()["rules_triggered"] == ["GEO_VELOCITY"]


def test_failed_logins_then_transaction_is_flagged(ctx):
    client, _ = ctx
    for _ in range(2):  # Lakeshore flags at 2 failed attempts
        login(client, "LAKESHORE-2001", "wrong")
    h = headers_for(client, "LAKESHORE-2001")
    r = send(client, h, account_id="LAKESHORE-2001", amount=50)
    assert r.json()["rules_triggered"] == ["FAILED_LOGINS"]


def test_cannot_transact_on_someone_elses_account(ctx):
    client, _ = ctx
    r = send(client, headers_for(client, "MAPLE-1001"), account_id="LAKESHORE-2001")
    assert r.status_code == 403


def test_invalid_payload_is_rejected(ctx):
    client, _ = ctx
    h = headers_for(client, "MAPLE-1001")
    assert send(client, h, amount=-5).status_code == 422
    assert send(client, h, transaction_type="refund").status_code == 422


def test_thresholds_endpoint_shows_bank_settings(ctx):
    client, _ = ctx
    r = client.get("/accounts/me/thresholds", headers=headers_for(client, "LAKESHORE-2001"))
    assert r.json()["large_amount_threshold"] == 2000
