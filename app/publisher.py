"""
Sends flagged-transaction events onward.

  * SqsPublisher - used in AWS: puts the event on the SQS queue the Lambda listens to
  * LogPublisher - used locally: just logs the event (and keeps it for tests)
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

logger = logging.getLogger("publisher")


class EventPublisher(Protocol):
    def publish(self, event: dict) -> None: ...


class SqsPublisher:
    def __init__(self, queue_url: str) -> None:
        import boto3

        self.queue_url = queue_url
        self.sqs = boto3.client("sqs")

    def publish(self, event: dict) -> None:
        self.sqs.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps(event))


class LogPublisher:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def publish(self, event: dict) -> None:
        self.sent.append(event)
        logger.warning("LOCAL MODE - flagged transaction event: %s", json.dumps(event))
