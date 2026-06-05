-- Phase 1: Add vehicle_type as an independent classification axis.
--
-- Asset Class  = what economic exposure it provides (Equity, Fixed Income, Cash, Commodity)
-- Vehicle Type = how it's structured (Stock, ETF, Mutual Fund, Trust/CEF, Bond/CD)
-- Sector       = equity-style economic theme (Energy, Technology, Precious Metals, etc.)
--               NULL for Fixed Income and Cash — they have no meaningful equity sector.

-- ── Seed vehicle_type in dim_symbols from yfinance asset_class ─────────────────
UPDATE dim_symbols SET vehicle_type = 'Bond/CD' WHERE asset_class = 'Bond'   AND vehicle_type IS NULL;
UPDATE dim_symbols SET vehicle_type = 'ETF'     WHERE asset_class = 'ETF'    AND vehicle_type IS NULL;
UPDATE dim_symbols SET vehicle_type = 'Stock'   WHERE asset_class = 'Equity' AND vehicle_type IS NULL;

-- ── Fix existing overrides: add vehicle_type and correct asset_class ───────────

-- Individual equity stocks (currently sector-only overrides)
UPDATE dim_symbol_overrides
SET vehicle_type = 'Stock', asset_class = 'Equity'
WHERE symbol IN ('AEM', 'FNV', 'HMY', 'KGC', 'NFGC', 'SBSW', 'WPM');

-- Physical metal trusts (hold bullion, not equities → Commodity)
UPDATE dim_symbol_overrides
SET vehicle_type = 'Trust/CEF', asset_class = 'Commodity'
WHERE symbol IN ('CEF', 'PSLV');

-- Commodity ETFs: hold futures or physical commodity
UPDATE dim_symbol_overrides
SET vehicle_type = 'ETF', asset_class = 'Commodity'
WHERE symbol IN ('CPER', 'PPLT', 'UNG', 'USO');

-- Equity ETFs: hold equities (miners, producers)
UPDATE dim_symbol_overrides
SET vehicle_type = 'ETF', asset_class = 'Equity'
WHERE symbol IN ('GDXJ', 'SILJ', 'URNM', 'XLE');

-- Money market fund → Cash
UPDATE dim_symbol_overrides
SET vehicle_type = 'Mutual Fund', asset_class = 'Cash'
WHERE symbol = 'IUSXX';

-- ── Insert / upsert overrides for symbols missing them ─────────────────────────
-- Ensure required custom sectors exist first
INSERT INTO dim_sectors (sector) VALUES ('Precious Metals'), ('Commodities')
ON CONFLICT (sector) DO NOTHING;

INSERT INTO dim_symbol_overrides (symbol, sector, asset_class, vehicle_type, notes, updated_at)
VALUES
    -- US Treasuries / T-bills: no equity sector
    ('06051YAC4', NULL,              'Fixed Income', 'Bond/CD',    'Corporate bond',                   NOW()),
    ('38151PNX5', NULL,              'Fixed Income', 'Bond/CD',    'Corporate bond',                   NOW()),
    ('912797PC5', NULL,              'Fixed Income', 'Bond/CD',    'US Treasury bill',                 NOW()),
    ('912797TD9', NULL,              'Fixed Income', 'Bond/CD',    'US Treasury bill',                 NOW()),
    ('912797TQ0', NULL,              'Fixed Income', 'Bond/CD',    'US Treasury bill',                 NOW()),
    ('912797UL9', NULL,              'Fixed Income', 'Bond/CD',    'US Treasury bill',                 NOW()),
    -- Gold ETF (holds physical bullion)
    ('GLD',       'Precious Metals', 'Commodity',    'ETF',         'Gold ETF (holds bullion)',         NOW()),
    -- Bond ETF
    ('STIP',      NULL,              'Fixed Income', 'ETF',         'TIPS ETF (inflation-linked bonds)',NOW()),
    -- Commodity ETFs
    ('WEAT',      'Commodities',     'Commodity',    'ETF',         'Wheat futures ETF',               NOW()),
    ('COPP',      'Commodities',     'Commodity',    'ETF',         'Copper ETC (physical)',            NOW()),
    -- Copper miners ETF (holds equities)
    ('COPX',      'Commodities',     'Equity',       'ETF',         'Copper miners ETF',               NOW()),
    -- VIX futures ETF
    ('UVXY',      NULL,              'Equity',       'ETF',         'Short-term VIX futures ETF',      NOW())
ON CONFLICT (symbol) DO UPDATE SET
    sector       = COALESCE(dim_symbol_overrides.sector,       EXCLUDED.sector),
    asset_class  = COALESCE(dim_symbol_overrides.asset_class,  EXCLUDED.asset_class),
    vehicle_type = COALESCE(dim_symbol_overrides.vehicle_type, EXCLUDED.vehicle_type),
    notes        = COALESCE(dim_symbol_overrides.notes,        EXCLUDED.notes),
    updated_at   = NOW();
