CREATE TABLE stamp_correction_audit (
  id INTEGER PRIMARY KEY CHECK (id > 0),
  request_id TEXT NOT NULL UNIQUE CHECK (length(request_id) = 32 AND request_id NOT GLOB '*[^0-9a-f]*'),
  customer_id TEXT NOT NULL REFERENCES customers(id),
  admin_actor TEXT NOT NULL CHECK (length(admin_actor) BETWEEN 1 AND 64),
  corrected_at INTEGER NOT NULL CHECK (typeof(corrected_at) = 'integer'),
  correction_type TEXT NOT NULL CHECK (correction_type IN ('remove', 'replace')),
  reason_code TEXT NOT NULL,
  reason_detail TEXT NOT NULL CHECK (length(trim(reason_detail)) BETWEEN 10 AND 300),
  old_product_id TEXT NOT NULL REFERENCES products(id),
  new_product_id TEXT REFERENCES products(id),
  original_collected_at INTEGER NOT NULL CHECK (typeof(original_collected_at) = 'integer'),
  stamp_count_after INTEGER NOT NULL CHECK (typeof(stamp_count_after) = 'integer' AND stamp_count_after BETWEEN 0 AND 10),
  eligible_before INTEGER NOT NULL CHECK (eligible_before IN (0, 1)),
  eligible_after INTEGER NOT NULL CHECK (eligible_after = (stamp_count_after = 10) AND eligible_after >= eligible_before),
  CHECK ((correction_type = 'remove' AND reason_code = 'erroneous_stamp' AND new_product_id IS NULL)
      OR (correction_type = 'replace' AND reason_code = 'wrong_product' AND new_product_id IS NOT NULL
          AND old_product_id <> new_product_id))
);
CREATE INDEX corrections_by_card ON stamp_correction_audit(customer_id, id);
CREATE TRIGGER protect_correction_update BEFORE UPDATE ON stamp_correction_audit
BEGIN
  SELECT RAISE(ABORT, 'correction_audit_immutable');
END;
CREATE TRIGGER protect_correction_delete BEFORE DELETE ON stamp_correction_audit
BEGIN
  SELECT RAISE(ABORT, 'correction_audit_immutable');
END;
CREATE TRIGGER protect_correction_replace BEFORE INSERT ON stamp_correction_audit
WHEN EXISTS (SELECT 1 FROM stamp_correction_audit WHERE request_id = NEW.request_id OR id = NEW.id)
BEGIN
  SELECT RAISE(ABORT, 'correction_audit_immutable');
END;
