-- Force recomputation of all dedupe_signatures with the narrowed business-key logic.
-- The new dedupe_payload excludes settlement_date, description, description2, and fee,
-- which always differ between CSV and API representations of the same trade.
-- Run build-ledger after migrate to trigger backfill_transaction_provenance().
UPDATE transactions SET dedupe_signature = NULL;
