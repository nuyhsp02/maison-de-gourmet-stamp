CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products (
  id TEXT PRIMARY KEY,
  qr_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  redeemed_at INTEGER
);
CREATE TABLE IF NOT EXISTS customer_profiles (
  customer_id TEXT PRIMARY KEY NOT NULL REFERENCES customers(id),
  display_name TEXT NOT NULL,
  phone_lookup_hmac TEXT NOT NULL UNIQUE,
  notice_version TEXT NOT NULL,
  notice_acknowledged_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL REFERENCES customers(id),
  csrf TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_customer ON sessions(customer_id);
CREATE TABLE IF NOT EXISTS stamps (
  customer_id TEXT NOT NULL REFERENCES customers(id),
  product_id TEXT NOT NULL REFERENCES products(id),
  collected_at INTEGER NOT NULL,
  PRIMARY KEY (customer_id, product_id)
);
CREATE TABLE IF NOT EXISTS recovery (
  customer_id TEXT PRIMARY KEY REFERENCES customers(id),
  token_hash TEXT NOT NULL UNIQUE,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reward_audit (
  customer_id TEXT PRIMARY KEY REFERENCES customers(id),
  redeemed_at INTEGER NOT NULL,
  method TEXT NOT NULL CHECK (method = 'unauthenticated_customer_screen')
);
CREATE TABLE IF NOT EXISTS admin_sessions (
  token_hash TEXT PRIMARY KEY NOT NULL,
  csrf TEXT NOT NULL,
  admin_actor TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stamp_cancel_audit (
  id INTEGER PRIMARY KEY,
  customer_id TEXT NOT NULL REFERENCES customers(id),
  product_id TEXT NOT NULL REFERENCES products(id),
  original_collected_at INTEGER NOT NULL,
  cancelled_at INTEGER NOT NULL,
  admin_actor TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cancellations_by_stamp ON stamp_cancel_audit(customer_id, product_id, id);
CREATE TRIGGER IF NOT EXISTS protect_redeemed_stamp
BEFORE DELETE ON stamps
WHEN (SELECT redeemed_at FROM customers WHERE id = OLD.customer_id) IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'redeemed_stamp_locked');
END;
