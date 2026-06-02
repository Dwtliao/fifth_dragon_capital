import json
from datetime import datetime, timezone

from pyetrade.accounts import ETradeAccounts

from etrade_sync.auth import load_tokens
from etrade_sync.config import CONSUMER_KEY, CONSUMER_SECRET, DEV
from etrade_sync.db import get_connection

BASE_URL = f"https://{'apisb' if DEV else 'api'}.etrade.com"


def _epoch_to_ts(epoch_secs):
    """Convert non-zero Unix epoch seconds to datetime, else None."""
    if not epoch_secs:
        return None
    return datetime.fromtimestamp(epoch_secs, tz=timezone.utc)


def _get_client():
    token, secret = load_tokens()
    return ETradeAccounts(CONSUMER_KEY, CONSUMER_SECRET, token, secret, dev=DEV)


def _list_accounts(client):
    resp = client.session.get(
        f"{BASE_URL}/v1/accounts/list.json",
        timeout=(5, 15),
    )
    resp.raise_for_status()
    data = resp.json()
    accounts = (
        data.get("AccountListResponse", {})
        .get("Accounts", {})
        .get("Account", [])
    )
    return accounts if isinstance(accounts, list) else [accounts]


def sync_accounts(account_filter=None, only=None):
    if only is not None and only != "accounts":
        return

    client = _get_client()
    accounts = _list_accounts(client)
    if account_filter:
        accounts = [a for a in accounts if a["accountIdKey"] == account_filter]

    upsert_sql = """
        INSERT INTO accounts
            (account_id_key, account_id, account_name, account_desc, account_mode,
             account_type, institution_type, status, closed_date, raw, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (account_id_key) DO UPDATE SET
            account_id       = EXCLUDED.account_id,
            account_name     = EXCLUDED.account_name,
            account_desc     = EXCLUDED.account_desc,
            account_mode     = EXCLUDED.account_mode,
            account_type     = EXCLUDED.account_type,
            institution_type = EXCLUDED.institution_type,
            status           = EXCLUDED.status,
            closed_date      = EXCLUDED.closed_date,
            raw              = EXCLUDED.raw,
            updated_at       = NOW()
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            for acct in accounts:
                cur.execute(upsert_sql, (
                    acct["accountIdKey"],
                    acct.get("accountId"),
                    acct.get("accountName"),
                    acct.get("accountDesc"),
                    acct.get("accountMode"),
                    acct.get("accountType"),
                    acct.get("institutionType"),
                    acct.get("accountStatus"),
                    _epoch_to_ts(acct.get("closedDate")),
                    json.dumps(acct),
                ))
    print(f"  accounts: upserted {len(accounts)} row(s)")


def sync_balances(account_filter=None, only=None):
    if only is not None and only != "balances":
        return

    client = _get_client()
    accounts = _list_accounts(client)
    if account_filter:
        accounts = [a for a in accounts if a["accountIdKey"] == account_filter]

    insert_sql = """
        INSERT INTO balances
            (account_id_key, cash_available_for_invest, cash_available_for_withdraw,
             net_cash, cash_balance, total_account_value, net_mv, net_mv_long, raw)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            count = 0
            for acct in accounts:
                key = acct["accountIdKey"]
                try:
                    resp = client.session.get(
                        f"{BASE_URL}/v1/accounts/{key}/balance.json",
                        params={"realTimeNAV": "true", "instType": acct["institutionType"]},
                        timeout=15,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    print(f"  balances: skipping {key} — {e}")
                    continue

                computed = data.get("BalanceResponse", {}).get("Computed", {})
                rtv = computed.get("RealTimeValues", {})

                cur.execute(insert_sql, (
                    key,
                    computed.get("cashAvailableForInvestment"),
                    computed.get("cashAvailableForWithdrawal"),
                    computed.get("netCash"),
                    computed.get("cashBalance"),
                    rtv.get("totalAccountValue"),
                    rtv.get("netMv"),
                    rtv.get("netMvLong"),
                    json.dumps(data),
                ))
                count += 1
    print(f"  balances: inserted {count} snapshot(s)")
