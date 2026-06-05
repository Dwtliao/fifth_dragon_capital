-- Exposure / theme tags: many-to-many between symbols and economic themes.
-- A symbol can have multiple tags (e.g. KGC → Precious Metals + Gold).
-- These are orthogonal to asset_class, vehicle_type, and sector — they capture
-- the economic theme or narrative driving the position.
-- Table created in 053b_symbol_exposure_tags_schema.sql (must precede mv_allocations).
-- This file adds indexes and seeds initial tag data.
CREATE INDEX IF NOT EXISTS set_symbol ON symbol_exposure_tags (symbol);
CREATE INDEX IF NOT EXISTS set_tag    ON symbol_exposure_tags (tag);

-- ── Seed initial tags for current holdings ─────────────────────────────────────
INSERT INTO symbol_exposure_tags (symbol, tag) VALUES
    -- Uranium / Nuclear
    ('CCJ',  'Uranium'),
    ('UEC',  'Uranium'),
    ('UUUU', 'Uranium'),
    ('URNM', 'Uranium'),

    -- Precious Metals (broad: miners, royalties, ETFs, trusts)
    ('AEM',  'Precious Metals'),
    ('FNV',  'Precious Metals'),
    ('KGC',  'Precious Metals'),
    ('NFGC', 'Precious Metals'),
    ('SBSW', 'Precious Metals'),
    ('WPM',  'Precious Metals'),
    ('GLD',  'Precious Metals'),
    ('CEF',  'Precious Metals'),
    ('PSLV', 'Precious Metals'),
    ('PPLT', 'Precious Metals'),
    ('GDXJ', 'Precious Metals'),
    ('SILJ', 'Precious Metals'),

    -- Gold (specific subset of Precious Metals)
    ('AEM',  'Gold'),
    ('FNV',  'Gold'),
    ('KGC',  'Gold'),
    ('NFGC', 'Gold'),
    ('WPM',  'Gold'),
    ('GLD',  'Gold'),
    ('CEF',  'Gold'),
    ('GDXJ', 'Gold'),

    -- Silver
    ('PSLV', 'Silver'),
    ('SILJ', 'Silver'),
    ('SBSW', 'Silver'),
    ('CEF',  'Silver'),
    ('WPM',  'Silver'),

    -- Platinum / Palladium
    ('PPLT', 'Platinum'),

    -- Broad Energy
    ('XLE',  'Broad Energy'),
    ('CNQ',  'Broad Energy'),
    ('UNG',  'Broad Energy'),
    ('USO',  'Broad Energy'),

    -- Copper
    ('COPP', 'Copper'),
    ('COPX', 'Copper'),
    ('CPER', 'Copper'),

    -- Agriculture
    ('WEAT', 'Agriculture'),

    -- Volatility
    ('UVXY', 'Volatility')

ON CONFLICT (symbol, tag) DO NOTHING;
