"""Tests for the notification Lambda, using fake AWS clients."""

import json
import os
from decimal import Decimal

os.environ.setdefault("AWS_DEFAULT_REGION", "ca-central-1")
os.environ["FRAUD_TABLE_NAME"] = "FraudLogs"
os.environ["ALERT_TOPIC_ARN"] = "arn:aws:sns:ca-central-1:123456789012:alerts"
os.environ.pop("FIREHOSE_STREAM_NAME", None)

import handler

ALERT = {
    "event_type": "TRANSACTION_FLAGGED",
    "transaction_id": "txn_abc123",
    "account_id": "MAPLE-1001",
    "tenant_id": "MAPLE",
    "bank": "Maple Bank",
    "customer_name": "Alex Chen",
    "amount": 7500.0,
    "transaction_type": "withdrawal",
    "location": "Toronto, ON",
    "transaction_timestamp": "2026-10-24T15:00:00+00:00",
    "flagged_at": "2026-10-24T15:00:01+00:00",
    "risk_score": 40,
    "rules_triggered": ["LARGE_AMOUNT"],
    "reasons": ["Withdrawal of $7,500.00 is above Maple Bank's limit of $5,000.00"],
}


class FakeTable:
    def __init__(self):
        self.items = []

    def put_item(self, Item):
        self.items.append(Item)


class FakeDynamo:
    def __init__(self):
        self.table = FakeTable()

    def Table(self, name):
        return self.table


class FakeSns:
    def __init__(self, fail=False):
        self.messages, self.fail = [], fail

    def publish(self, **kwargs):
        if self.fail:
            raise RuntimeError("SNS is down")
        self.messages.append(kwargs)


def setup_fakes(monkeypatch, sns_fail=False):
    dynamo, sns = FakeDynamo(), FakeSns(fail=sns_fail)
    monkeypatch.setattr(handler, "_clients", {"dynamodb": dynamo, "sns": sns})
    return dynamo, sns


def sqs_event(*bodies):
    return {"Records": [{"messageId": f"m{i}", "body": json.dumps(b)} for i, b in enumerate(bodies)]}


def test_flagged_transaction_is_saved_and_customer_notified(monkeypatch):
    dynamo, sns = setup_fakes(monkeypatch)
    result = handler.handler(sqs_event(ALERT), None)

    assert result == {"batchItemFailures": []}
    saved = dynamo.table.items[0]
    assert saved["transaction_id"] == "txn_abc123"
    assert saved["amount"] == Decimal("7500.0")  # DynamoDB needs Decimal, not float
    assert saved["review_status"] == "PENDING_REVIEW"
    assert "Alex Chen" in sns.messages[0]["Message"]
    assert len(sns.messages[0]["Subject"]) <= 100


def test_failed_message_is_reported_for_retry(monkeypatch):
    setup_fakes(monkeypatch, sns_fail=True)
    result = handler.handler(sqs_event(ALERT), None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m0"}]}


def test_bad_message_does_not_block_good_ones(monkeypatch):
    dynamo, _ = setup_fakes(monkeypatch)
    event = sqs_event(ALERT)
    event["Records"].insert(0, {"messageId": "bad", "body": "not json"})
    result = handler.handler(event, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
    assert len(dynamo.table.items) == 1


def test_message_text():
    subject, body = handler.build_alert_message(ALERT)
    assert "MAPLE-1001" in subject
    assert "$7,500.00" in body
    assert "above Maple Bank's limit" in body
