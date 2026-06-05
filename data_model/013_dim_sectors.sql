CREATE TABLE IF NOT EXISTS dim_sectors (
    sector      TEXT PRIMARY KEY,
    sort_order  INT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO dim_sectors (sector, sort_order) VALUES
    ('Technology',             1),
    ('Communication Services', 2),
    ('Consumer Discretionary', 3),
    ('Consumer Staples',       4),
    ('Energy',                 5),
    ('Financials',             6),
    ('Healthcare',             7),
    ('Industrials',            8),
    ('Materials',              9),
    ('Real Estate',           10),
    ('Utilities',             11),
    ('Fixed Income',          12),
    ('Commodities',           13),
    ('Cash & Equivalents',    14),
    ('Other',                 15)
ON CONFLICT (sector) DO NOTHING;
