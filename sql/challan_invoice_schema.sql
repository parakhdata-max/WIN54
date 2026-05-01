-- =====================================================
-- CHALLAN & INVOICE MANAGEMENT SCHEMA
-- =====================================================

-- 1. Update Party Master to include billing preference
ALTER TABLE parties ADD COLUMN IF NOT EXISTS billing_preference VARCHAR(20) DEFAULT 'CHALLAN';
-- Values: 'CHALLAN' or 'DIRECT_INVOICE'

-- 2. Challan Master Table
CREATE TABLE IF NOT EXISTS challans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    challan_no VARCHAR(50) UNIQUE NOT NULL,
    party_id UUID REFERENCES parties(id),
    order_ids TEXT[], -- Array of order IDs included in this challan
    challan_date DATE DEFAULT CURRENT_DATE,
    total_amount DECIMAL(12,2) DEFAULT 0,
    total_tax DECIMAL(12,2) DEFAULT 0,
    grand_total DECIMAL(12,2) DEFAULT 0,
    status VARCHAR(20) DEFAULT 'PENDING', -- PENDING, INVOICED, CANCELLED
    created_by VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    remarks TEXT
);

-- 3. Invoice Master Table
CREATE TABLE IF NOT EXISTS invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_no VARCHAR(50) UNIQUE NOT NULL,
    challan_id UUID REFERENCES challans(id), -- NULL for direct invoices
    party_id UUID REFERENCES parties(id),
    order_ids TEXT[], -- Array of order IDs included in this invoice
    invoice_date DATE DEFAULT CURRENT_DATE,
    due_date DATE,
    total_amount DECIMAL(12,2) DEFAULT 0,
    total_tax DECIMAL(12,2) DEFAULT 0,
    grand_total DECIMAL(12,2) DEFAULT 0,
    status VARCHAR(20) DEFAULT 'PENDING', -- PENDING, PAID, OVERDUE, CANCELLED
    payment_status VARCHAR(20) DEFAULT 'UNPAID', -- UNPAID, PARTIAL, PAID
    created_by VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    remarks TEXT
);

-- 4. Challan Line Items (for tracking individual order lines)
CREATE TABLE IF NOT EXISTS challan_lines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    challan_id UUID REFERENCES challans(id) ON DELETE CASCADE,
    order_id UUID REFERENCES orders(id),
    order_line_id UUID REFERENCES order_lines(id),
    product_name VARCHAR(200),
    brand VARCHAR(100),
    eye_side CHAR(1),
    quantity INTEGER,
    unit_price DECIMAL(10,2),
    total_price DECIMAL(12,2),
    gst_percent DECIMAL(5,2) DEFAULT 0,
    gst_amount  DECIMAL(12,2) DEFAULT 0,
    line_total  DECIMAL(12,2),
    is_deleted  BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4a. Order charges (Fitting, Colouring, Courier etc.)
CREATE TABLE IF NOT EXISTS order_charges (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id        UUID NOT NULL,
    charge_type     TEXT NOT NULL,
    description     TEXT,
    amount          NUMERIC(12,2) NOT NULL DEFAULT 0,
    gst_percent     NUMERIC(5,2)  NOT NULL DEFAULT 0,
    gst_amount      NUMERIC(12,2) GENERATED ALWAYS AS
                        (ROUND(amount * gst_percent / 100, 2)) STORED,
    total_amount    NUMERIC(12,2) GENERATED ALWAYS AS
                        (ROUND(amount + amount * gst_percent / 100, 2)) STORED,
    courier_company TEXT,
    tracking_no     TEXT,
    challan_id      UUID REFERENCES challans(id),
    is_confirmed    BOOLEAN DEFAULT TRUE,
    is_locked       BOOLEAN DEFAULT FALSE,
    created_by      TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 4b. Service charges snapshot per challan
CREATE TABLE IF NOT EXISTS challan_service_charges (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    challan_id      UUID NOT NULL,
    order_id        UUID,
    charge_type     TEXT,
    description     TEXT,
    base_amount     NUMERIC(12,2),
    gst_percent     NUMERIC(5,2),
    gst_amount      NUMERIC(12,2),
    total_amount    NUMERIC(12,2),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 4c. Courier companies lookup
CREATE TABLE IF NOT EXISTS courier_companies (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name      TEXT UNIQUE NOT NULL,
    is_active BOOLEAN DEFAULT TRUE
);

-- 5. Invoice Line Items
CREATE TABLE IF NOT EXISTS invoice_lines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID REFERENCES invoices(id) ON DELETE CASCADE,
    challan_line_id UUID REFERENCES challan_lines(id),
    order_id UUID REFERENCES orders(id),
    order_line_id UUID REFERENCES order_lines(id),
    product_name VARCHAR(200),
    brand VARCHAR(100),
    eye_side CHAR(1),
    quantity INTEGER,
    unit_price DECIMAL(10,2),
    total_price DECIMAL(12,2),
    tax_rate DECIMAL(5,2) DEFAULT 0,
    tax_amount DECIMAL(12,2) DEFAULT 0,
    line_total DECIMAL(12,2),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 6. Indexes for performance
CREATE INDEX IF NOT EXISTS idx_challans_party ON challans(party_id);
CREATE INDEX IF NOT EXISTS idx_challans_status ON challans(status);
CREATE INDEX IF NOT EXISTS idx_challans_date ON challans(challan_date);
CREATE INDEX IF NOT EXISTS idx_invoices_party ON invoices(party_id);
CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoices_payment ON invoices(payment_status);
CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(invoice_date);

-- 7. Function to generate challan numbers
CREATE OR REPLACE FUNCTION generate_challan_no()
RETURNS TEXT AS $$
DECLARE
    year_part TEXT;
    seq_part TEXT;
    challan_no TEXT;
BEGIN
    year_part := EXTRACT(YEAR FROM CURRENT_DATE)::TEXT;
    seq_part := LPAD(nextval('challan_seq')::TEXT, 4, '0');
    challan_no := 'CH/' || year_part || '/' || seq_part;
    RETURN challan_no;
END;
$$ LANGUAGE plpgsql;

-- 8. Function to generate invoice numbers
CREATE OR REPLACE FUNCTION generate_invoice_no()
RETURNS TEXT AS $$
DECLARE
    year_part TEXT;
    seq_part TEXT;
    invoice_no TEXT;
BEGIN
    year_part := EXTRACT(YEAR FROM CURRENT_DATE)::TEXT;
    seq_part := LPAD(nextval('invoice_seq')::TEXT, 4, '0');
    invoice_no := 'INV/' || year_part || '/' || seq_part;
    RETURN invoice_no;
END;
$$ LANGUAGE plpgsql;

-- 9. Create sequences if they don't exist
CREATE SEQUENCE IF NOT EXISTS challan_seq START 1;
CREATE SEQUENCE IF NOT EXISTS invoice_seq START 1;

-- 10. Update trigger for timestamps
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_challans_updated_at 
    BEFORE UPDATE ON challans 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_invoices_updated_at 
    BEFORE UPDATE ON invoices 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- 11. Sample data for testing (optional)
-- UPDATE parties SET billing_preference = 'CHALLAN' WHERE id = 'some-party-uuid';
-- UPDATE parties SET billing_preference = 'DIRECT_INVOICE' WHERE id = 'some-other-party-uuid';
