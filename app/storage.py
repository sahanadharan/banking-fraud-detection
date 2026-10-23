"""
Where the API remembers account history between requests.

Two interchangeable implementations:
  * DynamoStateStore  - used in AWS (shared by every ECS task)
  * MemoryStateStore  - used when running locally / in tests (no AWS needed)
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Protocol

from fraud_rules import AccountHistory

MAX_FAILED_LOGINS_KEPT = 20


class StateStore(Protocol):
    def get_history(self, account_id: str) -> AccountHistory: ...
    def record_transaction(self, account_id: str, location: str, timestamp: datetime) -> None: ...
    def record_failed_login(self, account_id: str, timestamp: datetime) -> None: ...


class MemoryStateStore:
    def __init__(self) -> None:
        self._data: dict[str, AccountHistory] = {}
        self._lock = threading.Lock()

    def get_history(self, account_id: str) -> AccountHistory:
        with self._lock:
            h = self._data.get(account_id, AccountHistory())
            return AccountHistory(h.last_location, h.last_timestamp, list(h.recent_failed_logins))

    def record_transaction(self, account_id: str, location: str, timestamp: datetime) -> None:
        with self._lock:
            h = self._data.setdefault(account_id, AccountHistory())
            h.last_location = location
            h.last_timestamp = timestamp

    def record_failed_login(self, account_id: str, timestamp: datetime) -> None:
        with self._lock:
            h = self._data.setdefault(account_id, AccountHistory())
            h.recent_failed_logins = (h.recent_failed_logins + [timestamp])[-MAX_FAILED_LOGINS_KEPT:]


class DynamoStateStore:
    """Item shape: {account_id, last_location, last_timestamp, failed_logins: [iso, ...]}"""

    def __init__(self, table_name: str) -> None:
        import boto3  # imported here so local mode doesn't need AWS libraries configured

        self.table = boto3.resource("dynamodb").Table(table_name)

    def get_history(self, account_id: str) -> AccountHistory:
        item = self.table.get_item(Key={"account_id": account_id}).get("Item") or {}
        last_ts = item.get("last_timestamp")
        return AccountHistory(
            last_location=item.get("last_location"),
            last_timestamp=datetime.fromisoformat(last_ts) if last_ts else None,
            recent_failed_logins=[datetime.fromisoformat(t) for t in item.get("failed_logins", [])],
        )

    def record_transaction(self, account_id: str, location: str, timestamp: datetime) -> None:
        self.table.update_item(
            Key={"account_id": account_id},
            UpdateExpression="SET last_location = :loc, last_timestamp = :ts",
            ExpressionAttributeValues={":loc": location, ":ts": timestamp.isoformat()},
        )

    def record_failed_login(self, account_id: str, timestamp: datetime) -> None:
        current = self.get_history(account_id).recent_failed_logins
        kept = [t.isoformat() for t in current + [timestamp]][-MAX_FAILED_LOGINS_KEPT:]
        self.table.update_item(
            Key={"account_id": account_id},
            UpdateExpression="SET failed_logins = :f",
            ExpressionAttributeValues={":f": kept},
        )
