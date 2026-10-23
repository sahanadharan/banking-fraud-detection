"""
Customer Notification & Alerting Lambda.

Triggered by the SQS queue of flagged transactions. For each message it:
  1. Saves the flagged transaction to the FraudLogs DynamoDB table (record-keeping / analytics)
  2. Sends a customer alert through Amazon SNS (email subscribers) and logs it
  3. (Optional bonus) Streams the record to Kinesis Data Firehose -> S3 for future ML analysis

If one message in a batch fails, only that message is retried (partial batch
response). After 3 failed attempts SQS moves it to the dead-letter queue.

Environment variables
  FRAUD_TABLE_NAME      DynamoDB table name (required)
  ALERT_TOPIC_ARN       SNS topic for customer alerts (required)
  FIREHOSE_STREAM_NAME  Firehose delivery stream (optional)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_clients: dict = {}


def _client(name: str):
    """Create AWS clients once per Lambda container and reuse them."""
    if name not in _clients:
        _clients[name] = boto3.resource("dynamodb") if name == "dynamodb" else boto3.client(name)
    return _clients[name]


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Not JSON serialisable: {type(value)}")


# ------------------------------------------------------------------ message
def build_alert_message(alert: dict) -> tuple[str, str]:
    """Return (subject, body) for the customer notification."""
    amount = float(alert["amount"])
    subject = f"Fraud alert: please review a transaction on account {alert['account_id']}"
    reasons = "\n".join(f"  - {r}" for r in alert.get("reasons", []))
    body = (
        f"Hi {alert.get('customer_name', 'Valued customer')},\n\n"
        f"{alert.get('bank', 'Your bank')} flagged a recent transaction on your account "
        f"{alert['account_id']} for review.\n\n"
        f"  Amount:    ${amount:,.2f} ({alert['transaction_type']})\n"
        f"  Location:  {alert['location']}\n"
        f"  Time:      {alert['transaction_timestamp']}\n"
        f"  Reference: {alert['transaction_id']}\n\n"
        f"Why it was flagged:\n{reasons}\n\n"
        "If you made this transaction, no action is needed.\n"
        "If you did not, please contact your bank immediately.\n"
    )
    return subject[:100], body  # SNS subjects are limited to 100 characters


# ------------------------------------------------------------------ steps
def save_record(alert: dict) -> None:
    table = _client("dynamodb").Table(os.environ["FRAUD_TABLE_NAME"])
    item = dict(alert)
    item["processed_at"] = datetime.now(timezone.utc).isoformat()
    item["review_status"] = "PENDING_REVIEW"
    # put_item overwrites by transaction_id, so a duplicate SQS delivery is harmless.
    table.put_item(Item=item)


def send_notification(alert: dict) -> None:
    subject, body = build_alert_message(alert)
    logger.warning("FRAUD ALERT %s", json.dumps(alert, default=_json_default))
    _client("sns").publish(
        TopicArn=os.environ["ALERT_TOPIC_ARN"],
        Subject=subject,
        Message=body,
        MessageAttributes={
            "tenant_id": {"DataType": "String", "StringValue": str(alert.get("tenant_id", ""))},
        },
    )


def stream_for_analytics(alert: dict) -> None:
    stream = os.environ.get("FIREHOSE_STREAM_NAME")
    if not stream:
        return
    _client("firehose").put_record(
        DeliveryStreamName=stream,
        Record={"Data": (json.dumps(alert, default=_json_default) + "\n").encode()},
    )


def process(alert: dict) -> None:
    save_record(alert)
    send_notification(alert)
    stream_for_analytics(alert)


# ------------------------------------------------------------------ entry point
def handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            # parse_float=Decimal because DynamoDB does not accept Python floats
            alert = json.loads(record["body"], parse_float=Decimal)
            process(alert)
            logger.info("Processed flagged transaction %s", alert.get("transaction_id"))
        except Exception:
            logger.exception("Failed to process message %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
