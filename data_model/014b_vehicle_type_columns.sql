-- Add vehicle_type column to symbol tables.
-- Must run before 053_mv_allocations.sql which references these columns.
-- Data seeding and override corrections are in 061_add_vehicle_type.sql.
ALTER TABLE dim_symbols          ADD COLUMN IF NOT EXISTS vehicle_type TEXT;
ALTER TABLE dim_symbol_overrides ADD COLUMN IF NOT EXISTS vehicle_type TEXT;
