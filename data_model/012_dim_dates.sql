-- Date spine: one row per calendar day covering full history + 1 year forward.
-- iso_week + iso_year are kept together to avoid week-1/week-52 ambiguity at
-- year boundaries (ISO week 1 of 2025 can start in late December 2024).
-- is_trading_day is based on NYSE calendar (weekdays minus US market holidays).
CREATE TABLE IF NOT EXISTS dim_dates (
    date_key       DATE PRIMARY KEY,
    year           INT,
    quarter        INT,
    month          INT,
    iso_week       INT,                 -- ISO 8601 week number (1–53)
    iso_year       INT,                 -- ISO year the week belongs to (differs from year near Jan 1)
    day_of_week    INT,                 -- 0=Monday … 6=Sunday
    is_weekend     BOOL,
    is_trading_day BOOL                 -- false on weekends and US NYSE market holidays
);
