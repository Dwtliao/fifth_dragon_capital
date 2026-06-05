import copy
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

_EVENT_TYPE_MAP = {
    "Bought":             "buy",
    "Sold":               "sell",
    "Dividend":           "dividend",
    "Qualified Dividend": "dividend_qualified",
    "Interest Income":    "interest",
    "Fee":                "fee",
    "Transfer":           "transfer",
    "Online Transfer":    None,
    "POS":                "withdrawal",
    "Bill Payment":       "withdrawal",
    "Stock Split":        "split",
    "Redemption":         "redemption",
}


def normalize_event_type(transaction_type, amount):
    mapped = _EVENT_TYPE_MAP.get((transaction_type or "").strip())
    if mapped is None:
        return "deposit" if (amount or 0) >= 0 else "withdrawal"
    return mapped


def normalize_symbol(symbol):
    if symbol is None:
        return None
    cleaned = str(symbol).strip().upper()
    return cleaned or None


def normalize_text(value):
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def normalize_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    cleaned = str(value).strip()
    return cleaned or None


def normalize_decimal(value, scale=None):
    if value is None or value == "":
        return None

    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None

    if scale is not None:
        quant = Decimal("1").scaleb(-scale)
        dec = dec.quantize(quant, rounding=ROUND_HALF_UP)

    normalized = dec.normalize()
    return format(normalized, "f")


def _stable_dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def _normalize_for_hash(value):
    if isinstance(value, dict):
        return {k: _normalize_for_hash(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_for_hash(v) for v in value]
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _hash_payload(value, drop_keys=None):
    if drop_keys:
        payload = copy.deepcopy(value)
        if isinstance(payload, dict):
            for key in drop_keys:
                payload.pop(key, None)
    else:
        payload = value

    return hashlib.sha256(_stable_dumps(_normalize_for_hash(payload)).encode("utf-8")).hexdigest()


def build_legacy_csv_transaction_id(account_id_key, transaction_date, transaction_type, symbol, quantity, price, description):
    """Legacy CSV transaction ID used by the pre-fingerprint importer."""
    key = "|".join([
        str(account_id_key),
        str(transaction_date),
        str(transaction_type),
        str(symbol),
        str(quantity),
        str(price),
        str(description)[:80],
    ])
    return "CSV:" + hashlib.sha1(key.encode("utf-8")).hexdigest()


def build_transaction_fingerprints(
    *,
    account_id_key,
    transaction_type,
    amount,
    symbol,
    transaction_date,
    settlement_date,
    quantity,
    price,
    fee,
    description=None,
    description2=None,
    source_payload,
    source_payload_drop_keys=None,
):
    """Return source and canonical fingerprints for a raw transaction row."""

    event_type = normalize_event_type(transaction_type, amount)
    canonical_payload = {
        "account_id_key": normalize_text(account_id_key),
        "event_type": event_type,
        "transaction_type": normalize_text(transaction_type),
        "symbol": normalize_symbol(symbol),
        "transaction_date": normalize_date(transaction_date),
        "settlement_date": normalize_date(settlement_date),
        "quantity": normalize_decimal(quantity),
        "price": normalize_decimal(price),
        "amount": normalize_decimal(amount),
        "fee": normalize_decimal(fee),
        "description": normalize_text(description),
        "description2": normalize_text(description2),
    }

    # dedupe_payload: narrow business-key for cross-source dedup (CSV vs API).
    # Excludes settlement_date (CSV uses T+2 synthetic; API returns actual),
    # description/description2 (always differ between CSV and API formats),
    # and fee (can be 0.0 vs 0 or absent). Only fields that are stable across
    # both source representations of the same trade.
    dedupe_payload = {
        "account_id_key": normalize_text(account_id_key),
        "event_type":     event_type,
        "symbol":         normalize_symbol(symbol),
        "transaction_date": normalize_date(transaction_date),
        "quantity":       normalize_decimal(quantity, 2),
        "price":          normalize_decimal(price, 2),
        "amount":         normalize_decimal(amount, 2),
    }

    return {
        "event_type": event_type,
        "source_payload_hash": _hash_payload(source_payload, drop_keys=source_payload_drop_keys),
        "canonical_fingerprint": _hash_payload(canonical_payload),
        "dedupe_signature": _hash_payload(dedupe_payload),
        "canonical_payload": canonical_payload,
        "dedupe_payload": dedupe_payload,
    }


def classify_group(peers, *, source_system, source_payload_hash):
    """Classify a transaction group for the ingest audit trail."""
    if not peers:
        return "canonical", "no matching peer rows"

    if len(peers) == 1:
        return "canonical", "only row in dedupe group"

    peer_sources = {p["source_system"] for p in peers if p["source_system"]}
    peer_payload_hashes = {p["source_payload_hash"] for p in peers if p["source_payload_hash"]}

    if len(peer_payload_hashes) == 1:
        return "exact_duplicate", "rows share the same source payload hash"

    if len(peer_sources) > 1:
        return "cross_source_overlap", "same dedupe signature spans API and CSV sources"

    if source_system in peer_sources and source_system:
        return "same_source_candidate", "same dedupe signature within one source"

    return "ambiguous", "same dedupe signature with no stronger duplicate signal"


def peer_lists(peers):
    return {
        "peer_transaction_ids": [p["transaction_id"] for p in peers],
        "peer_source_systems": sorted({p["source_system"] for p in peers if p["source_system"]}),
    }


def record_ingest_audit(
    cur,
    *,
    account_id_key,
    transaction_id,
    source_system,
    source_record_key,
    source_payload_hash,
    canonical_fingerprint,
    dedupe_signature,
    write_status,
    raw_payload,
):
    """Write a row-level ingest audit record and return the classification."""
    cur.execute(
        """
        SELECT transaction_id, source_system, source_record_key, source_payload_hash
        FROM transactions
        WHERE account_id_key = %s
          AND dedupe_signature = %s
        ORDER BY created_at, id
        """,
        (account_id_key, dedupe_signature),
    )
    peers = [
        {
            "transaction_id": row[0],
            "source_system": row[1],
            "source_record_key": row[2],
            "source_payload_hash": row[3],
        }
        for row in cur.fetchall()
    ]

    classification, reason = classify_group(
        peers,
        source_system=source_system,
        source_payload_hash=source_payload_hash,
    )
    peer_meta = peer_lists(peers)
    payload_summary = {
        "write_status": write_status,
        "classification": classification,
        "reason": reason,
        "current": {
            "transaction_id": transaction_id,
            "source_system": source_system,
            "source_record_key": source_record_key,
            "source_payload_hash": source_payload_hash,
            "canonical_fingerprint": canonical_fingerprint,
            "dedupe_signature": dedupe_signature,
        },
        "peers": peer_meta["peer_transaction_ids"],
        "raw_payload": raw_payload,
    }

    cur.execute(
        """
        INSERT INTO transaction_ingest_audit
            (account_id_key, transaction_id, source_system, source_record_key,
             source_payload_hash, canonical_fingerprint, dedupe_signature,
             write_status, classification, reason, peer_transaction_ids,
             peer_source_systems, payload_summary, observed_count, last_seen_at)
        VALUES
            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, NOW())
        ON CONFLICT (account_id_key, source_system, source_record_key) DO UPDATE SET
            transaction_id        = EXCLUDED.transaction_id,
            source_payload_hash   = EXCLUDED.source_payload_hash,
            canonical_fingerprint = EXCLUDED.canonical_fingerprint,
            dedupe_signature      = EXCLUDED.dedupe_signature,
            write_status          = EXCLUDED.write_status,
            classification        = EXCLUDED.classification,
            reason                = EXCLUDED.reason,
            peer_transaction_ids  = EXCLUDED.peer_transaction_ids,
            peer_source_systems   = EXCLUDED.peer_source_systems,
            payload_summary       = EXCLUDED.payload_summary,
            observed_count        = COALESCE(transaction_ingest_audit.observed_count, 1) + 1,
            last_seen_at          = NOW()
        """,
        (
            account_id_key,
            transaction_id,
            source_system,
            source_record_key,
            source_payload_hash,
            canonical_fingerprint,
            dedupe_signature,
            write_status,
            classification,
            reason,
            json.dumps(peer_meta["peer_transaction_ids"]),
            json.dumps(peer_meta["peer_source_systems"]),
            json.dumps(payload_summary),
        ),
    )

    return classification, reason, len(peers), peer_meta
