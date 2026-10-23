"""
Runs a scripted demo against the API and prints what happens.

  python scripts/demo.py --url http://<your-alb-dns-name>
  python scripts/demo.py --url http://localhost:8000          (local mode)

Uses fresh random account numbers each run, so results are repeatable.
Only uses the Python standard library - nothing to install.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

PASSWORD = "Demo@1234"
GREEN, RED, YELLOW, BOLD, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[1m", "\033[0m"


def call(base: str, method: str, path: str, body: dict | None = None, token: str | None = None):
    req = urllib.request.Request(base.rstrip("/") + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def login(base, account, password=PASSWORD):
    return call(base, "POST", "/auth/login", {"account_id": account, "password": password})


def token_for(base, account):
    status, body = login(base, account)
    assert status == 200, f"login failed: {status} {body}"
    return body["access_token"]


def transact(base, token, account, amount, ttype, location, ts=None):
    body = {"account_id": account, "amount": amount, "transaction_type": ttype, "location": location}
    if ts:
        body["timestamp"] = ts.isoformat()
    status, resp = call(base, "POST", "/transactions", body, token)
    if status != 200:
        print(f"    {YELLOW}HTTP {status}: {resp.get('detail')}{RESET}")
        return resp
    colour = GREEN if resp["status"] == "approved" else RED
    print(f"    ${amount:,.2f} {ttype} in {location} -> {colour}{BOLD}{resp['status'].upper()}{RESET}"
          f" (risk score {resp['risk_score']})")
    for reason in resp["reasons"]:
        print(f"      - {reason}")
    return resp


def heading(text):
    print(f"\n{BOLD}{text}{RESET}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="API base URL, e.g. http://my-alb.amazonaws.com")
    base = parser.parse_args().url
    if os.name == "nt":
        os.system("")  # turns on colour output in the Windows terminal

    run = random.randint(1000, 9999)
    maple_a, maple_b, maple_c = (f"MAPLE-{run}{i}" for i in (1, 2, 3))
    lake_a, lake_b = (f"LAKESHORE-{run}{i}" for i in (1, 2))
    now = datetime.now(timezone.utc)

    status, body = call(base, "GET", "/health")
    print(f"Health check: HTTP {status} {body}")

    heading("1. Normal activity is approved")
    t = token_for(base, maple_a)
    transact(base, t, maple_a, 120.00, "withdrawal", "Toronto, ON")
    transact(base, t, maple_a, 2500.00, "deposit", "Toronto, ON")

    heading("2. Unusually large withdrawal is flagged (Maple Bank limit: $5,000)")
    transact(base, t, maple_a, 7500.00, "withdrawal", "Toronto, ON")

    heading("3. Different cities within a short window ('impossible travel')")
    t = token_for(base, maple_b)
    transact(base, t, maple_b, 60.00, "withdrawal", "Toronto, ON", now - timedelta(minutes=20))
    transact(base, t, maple_b, 900.00, "transfer", "Vancouver, BC", now)

    heading("4. Repeated failed logins, then a transaction (possible account takeover)")
    for attempt in range(1, 4):
        status, _ = login(base, maple_c, "wrong-password")
        print(f"    Failed login attempt {attempt}: HTTP {status}")
    t = token_for(base, maple_c)
    print("    Correct password entered - login succeeds")
    transact(base, t, maple_c, 400.00, "transfer", "Toronto, ON")

    heading("5. Multi-tenant: the same $3,000 withdrawal at two different banks")
    t = token_for(base, maple_a)
    print("    Maple Bank (limit $5,000):")
    transact(base, t, maple_a, 3000.00, "withdrawal", "Toronto, ON")
    t = token_for(base, lake_a)
    print("    Lakeshore Credit Union (limit $2,000):")
    transact(base, t, lake_a, 3000.00, "withdrawal", "Toronto, ON")

    heading("6. Security: a customer cannot transact on someone else's account")
    t = token_for(base, lake_b)
    transact(base, t, maple_a, 50.00, "withdrawal", "Toronto, ON")
    status, body = call(base, "POST", "/transactions",
                        {"account_id": maple_a, "amount": 50, "transaction_type": "withdrawal",
                         "location": "Toronto, ON"})
    print(f"    No token at all: HTTP {status} - {body.get('detail')}")

    print(f"\n{BOLD}Done.{RESET} Flagged transactions are now in the FraudLogs DynamoDB table, "
          "and alert emails are on their way.")


if __name__ == "__main__":
    main()
