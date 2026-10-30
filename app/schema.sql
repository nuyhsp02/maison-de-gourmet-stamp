PRAGMA journal_mode = WAL;
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
