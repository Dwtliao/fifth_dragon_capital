-- Schema-only stub so mv_allocations (053) can reference symbol_exposure_tags.
-- Initial data seed and indexes are in 062_symbol_exposure_tags.sql.
CREATE TABLE IF NOT EXISTS symbol_exposure_tags (
    symbol  TEXT NOT NULL,
    tag     TEXT NOT NULL,
    PRIMARY KEY (symbol, tag)
);
