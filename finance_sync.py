"""
Daily finance sync: SimpleFIN Bridge -> Supabase (read-only bank/credit card data).

First run: put a SimpleFIN *setup token* in the SIMPLEFIN_SETUP_TOKEN secret. The script
claims it once, stores the resulting access URL in public.finance_config, and reuses it
after that (setup tokens are single-use). You can delete the secret afterwards.

Env vars (GitHub secrets):
  SUPABASE_URL, SUPABASE_SERVICE_KEY   required (same as the health sync)
  SIMPLEFIN_SETUP_TOKEN                needed only for the first run
  SIMPLEFIN_ACCESS_URL                 optional override instead of the stored one
  FINANCE_SYNC_DAYS                    default 14 (use up to 90 for a backfill)
  TZ_NAME                              default America/Denver
"""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo(os.getenv("TZ_NAME", "America/Denver"))
SYNC_DAYS = int(os.getenv("FINANCE_SYNC_DAYS") or "14")
CHUNK_DAYS = 60  # SimpleFIN caps how wide one request's date range can be
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HDRS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}


# ---------------------------------------------------------------- supabase

def sb_get(path: str, params: dict) -> list:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=HDRS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_upsert(table: str, rows: list[dict], on_conflict: str) -> int:
    if not rows:
        return 0
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={**HDRS, "Prefer": "resolution=merge-duplicates,return=minimal"},
        params={"on_conflict": on_conflict},
        data=json.dumps(rows),
        timeout=60,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"upsert {table} failed: {r.status_code} {r.text[:300]}")
    return len(rows)


def sb_insert(table: str, row: dict) -> None:
    requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers={**HDRS, "Prefer": "return=minimal"},
                  data=json.dumps(row), timeout=30).raise_for_status()


def sb_delete(table: str, params: dict) -> None:
    requests.delete(f"{SUPABASE_URL}/rest/v1/{table}", headers=HDRS, params=params, timeout=30).raise_for_status()


# ---------------------------------------------------------------- simplefin

def access_url() -> str | None:
    if os.getenv("SIMPLEFIN_ACCESS_URL"):
        return os.environ["SIMPLEFIN_ACCESS_URL"].strip()
    stored = sb_get("finance_config", {"key": "eq.simplefin_access_url", "select": "value"})
    if stored:
        return stored[0]["value"]
    token = os.getenv("SIMPLEFIN_SETUP_TOKEN", "").strip()
    if not token:
        print("Waiting for setup: add a SIMPLEFIN_SETUP_TOKEN secret, then re-run this workflow.")
        return None
    claim_url = base64.b64decode(token + "=" * (-len(token) % 4)).decode().strip()
    r = requests.post(claim_url, headers={"Content-Length": "0"}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Claiming the setup token failed ({r.status_code}). Tokens are single-use; make a new one.")
    url = r.text.strip()
    sb_upsert("finance_config", [{"key": "simplefin_access_url", "value": url,
                                  "updated_at": datetime.now(timezone.utc).isoformat()}], "key")
    print("Claimed SimpleFIN setup token and stored the access URL.")
    return url


def fetch_accounts(url: str, start: datetime, end: datetime) -> dict:
    parts = urlsplit(url)
    auth = (parts.username or "", parts.password or "")
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    base = urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))
    r = requests.get(f"{base}/accounts", auth=auth, timeout=90, params={
        "start-date": int(start.timestamp()), "end-date": int(end.timestamp()), "pending": 1})
    if r.status_code == 403:
        sys.exit("SimpleFIN rejected the access URL (403). Re-run with a new SIMPLEFIN_SETUP_TOKEN "
                 "after deleting the simplefin_access_url row in finance_config.")
    r.raise_for_status()
    return r.json()


def money(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def ts(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc) if epoch else None


# ---------------------------------------------------------------- main

def main() -> int:
    url = access_url()
    if not url:
        return 0
    now = datetime.now(timezone.utc)
    start_all = now - timedelta(days=SYNC_DAYS)

    accounts: dict[str, dict] = {}
    txns: dict[tuple, dict] = {}
    errors: list[str] = []

    chunk_start = start_all
    while chunk_start < now:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), now)
        data = fetch_accounts(url, chunk_start, chunk_end)
        errors += [str(e) for e in data.get("errors", [])]
        for a in data.get("accounts", []):
            accounts[a["id"]] = a  # last chunk = freshest balance
            for t in a.get("transactions", []):
                txns[(a["id"], t["id"])] = t
        chunk_start = chunk_end

    stamp = now.isoformat()
    acct_rows, hist_rows = [], []
    for a in accounts.values():
        org = a.get("org") or {}
        bdate = ts(a.get("balance-date"))
        acct_rows.append({
            "id": a["id"], "org_name": org.get("name"), "org_domain": org.get("domain"),
            "name": a.get("name") or a["id"], "currency": a.get("currency") or "USD",
            "balance": money(a.get("balance")), "available_balance": money(a.get("available-balance")),
            "balance_date": bdate.isoformat() if bdate else None, "updated_at": stamp,
        })
        hist_rows.append({
            "account_id": a["id"], "as_of_date": (bdate or now).astimezone(TZ).date().isoformat(),
            "balance": money(a.get("balance")), "available_balance": money(a.get("available-balance")),
        })

    txn_rows = []
    for (acct_id, _), t in txns.items():
        posted = ts(t.get("posted")) or ts(t.get("transacted_at"))
        trans = ts(t.get("transacted_at"))
        txn_rows.append({
            "account_id": acct_id, "id": t["id"],
            "posted_at": posted.isoformat() if posted else None,
            "posted_date": posted.astimezone(TZ).date().isoformat() if posted else None,
            "transacted_at": trans.isoformat() if trans else None,
            "amount": money(t.get("amount")) or 0,
            "description": t.get("description"), "payee": t.get("payee"), "memo": t.get("memo"),
            "pending": bool(t.get("pending")), "updated_at": stamp,
        })

    # Pending transactions get new ids once they post, so clear old pending rows in the window first
    for acct_id in accounts:
        sb_delete("finance_transactions", {"account_id": f"eq.{acct_id}", "pending": "eq.true",
                                           "posted_date": f"gte.{start_all.astimezone(TZ).date().isoformat()}"})

    sb_upsert("finance_accounts", acct_rows, "id")
    sb_upsert("finance_balance_history", hist_rows, "account_id,as_of_date")
    for i in range(0, len(txn_rows), 500):
        sb_upsert("finance_transactions", txn_rows[i:i + 500], "account_id,id")
    sb_insert("finance_sync_runs", {"accounts": len(acct_rows), "transactions": len(txn_rows),
                                    "errors": errors or None})

    print(f"Synced {len(acct_rows)} accounts, {len(txn_rows)} transactions over {SYNC_DAYS} days.")
    for e in errors:
        print("SimpleFIN:", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
