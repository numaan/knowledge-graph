ALTER TABLE orders ADD COLUMN status TEXT;
ALTER TABLE orders DROP COLUMN created_at;
