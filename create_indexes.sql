-- Run once in pgAdmin to speed up payment collection queries
-- Takes 1-5 seconds on small DB, run during off-hours for large DB

-- Party lookups
CREATE INDEX IF NOT EXISTS idx_parties_active      ON parties(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_parties_name        ON parties(party_name);
CREATE INDEX IF NOT EXISTS idx_parties_mobile      ON parties(mobile);
CREATE INDEX IF NOT EXISTS idx_patients_name       ON patients(master_name);

-- Invoice queries
CREATE INDEX IF NOT EXISTS idx_invoices_party      ON invoices(party_id);
CREATE INDEX IF NOT EXISTS idx_invoices_status     ON invoices(payment_status) WHERE payment_status != 'PAID';
CREATE INDEX IF NOT EXISTS idx_invoices_date       ON invoices(invoice_date DESC);

-- Challan queries  
CREATE INDEX IF NOT EXISTS idx_challans_party      ON challans(party_id);
CREATE INDEX IF NOT EXISTS idx_challans_status     ON challans(status) WHERE status NOT IN ('PAID','CANCELLED');
CREATE INDEX IF NOT EXISTS idx_challans_date       ON challans(challan_date DESC);

-- Order queries
CREATE INDEX IF NOT EXISTS idx_orders_party        ON orders(party_id);
CREATE INDEX IF NOT EXISTS idx_orders_party_name   ON orders(party_name);
CREATE INDEX IF NOT EXISTS idx_orders_patient_name ON orders(patient_name);
CREATE INDEX IF NOT EXISTS idx_orders_status       ON orders(status);

-- Payments
CREATE INDEX IF NOT EXISTS idx_payments_invoice    ON payments(invoice_id);
CREATE INDEX IF NOT EXISTS idx_payments_challan    ON payments(challan_id);
CREATE INDEX IF NOT EXISTS idx_payments_order      ON payments(advance_for_order_id);

ANALYZE invoices, challans, orders, parties, payments;

SELECT 'Indexes created successfully' AS status;
