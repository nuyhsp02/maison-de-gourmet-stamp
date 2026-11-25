CREATE TABLE customer_contacts (
  customer_id TEXT PRIMARY KEY NOT NULL REFERENCES customer_profiles(customer_id),
  sealed_phone BLOB NOT NULL,
  key_id TEXT NOT NULL,
  format_version INTEGER NOT NULL CHECK(format_version = 1),
  phone_masked TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE contact_access_audit (
  request_id TEXT PRIMARY KEY NOT NULL,
  requested_at INTEGER NOT NULL,
  actor TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  purpose TEXT NOT NULL CHECK(purpose IN ('pickup','reward','inquiry')),
  result TEXT NOT NULL CHECK(result IN ('unavailable','released','displayed','decrypt_failed'))
);
