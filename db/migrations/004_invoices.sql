-- 004_invoices.sql: partitioned invoices and lines, GST rules as triggers, views.

CREATE TYPE direction AS ENUM ('INWARD', 'OUTWARD');
CREATE TYPE doc_type AS ENUM ('INV', 'CRN', 'DBN');
CREATE TYPE itc_category AS ENUM ('ELIGIBLE', 'BLOCKED_17_5', 'INELIGIBLE');

-- fin_year is a plain column (PG forbids generated partition keys); the CHECK
-- keeps it consistent with invoice_date.
CREATE TABLE invoice (
    invoice_id      bigint GENERATED ALWAYS AS IDENTITY,
    gstin           gstin NOT NULL REFERENCES gstin_reg(gstin),
    party_gstin     gstin NOT NULL REFERENCES party(gstin),
    ref_invoice_id  bigint,
    ref_fin_year    text,
    direction       direction NOT NULL,
    doc_type        doc_type NOT NULL,
    invoice_no      text NOT NULL,
    invoice_no_norm text GENERATED ALWAYS AS (norm_doc_no(invoice_no)) STORED,
    invoice_date    date NOT NULL,
    fin_year        text NOT NULL,
    place_of_supply char(2) NOT NULL,
    is_rcm          boolean NOT NULL DEFAULT false,
    is_import       boolean NOT NULL DEFAULT false,
    PRIMARY KEY (invoice_id, fin_year),
    UNIQUE (gstin, party_gstin, direction, doc_type, invoice_no_norm, fin_year),
    CHECK (fin_year = fin_year_of(invoice_date)),
    CHECK ((ref_invoice_id IS NULL) = (ref_fin_year IS NULL)),
    FOREIGN KEY (ref_invoice_id, ref_fin_year) REFERENCES invoice(invoice_id, fin_year)
) PARTITION BY LIST (fin_year);

CREATE TABLE invoice_2025_26 PARTITION OF invoice FOR VALUES IN ('2025-26');
CREATE TABLE invoice_2026_27 PARTITION OF invoice FOR VALUES IN ('2026-27');
CREATE TABLE invoice_2027_28 PARTITION OF invoice FOR VALUES IN ('2027-28');
CREATE TABLE invoice_default PARTITION OF invoice DEFAULT;

CREATE INDEX invoice_party_idx ON invoice (party_gstin);

CREATE TABLE invoice_line (
    line_id       bigint GENERATED ALWAYS AS IDENTITY,
    invoice_id    bigint NOT NULL,
    fin_year      text NOT NULL,
    hsn_sac       text NOT NULL REFERENCES hsn_sac(code),
    taxable_value numeric(14,2) NOT NULL,
    rate          numeric(5,2) NOT NULL,
    igst          numeric(14,2) NOT NULL DEFAULT 0,
    cgst          numeric(14,2) NOT NULL DEFAULT 0,
    sgst          numeric(14,2) NOT NULL DEFAULT 0,
    cess          numeric(14,2) NOT NULL DEFAULT 0,
    itc_category  itc_category NOT NULL DEFAULT 'ELIGIBLE',
    PRIMARY KEY (line_id, fin_year),
    FOREIGN KEY (invoice_id, fin_year) REFERENCES invoice(invoice_id, fin_year)
) PARTITION BY LIST (fin_year);

CREATE TABLE invoice_line_2025_26 PARTITION OF invoice_line FOR VALUES IN ('2025-26');
CREATE TABLE invoice_line_2026_27 PARTITION OF invoice_line FOR VALUES IN ('2026-27');
CREATE TABLE invoice_line_2027_28 PARTITION OF invoice_line FOR VALUES IN ('2027-28');
CREATE TABLE invoice_line_default PARTITION OF invoice_line DEFAULT;

CREATE INDEX invoice_line_inv_idx ON invoice_line (invoice_id, fin_year);

CREATE TABLE payment (
    payment_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoice_id bigint NOT NULL,
    fin_year   text NOT NULL,
    paid_on    date NOT NULL,
    amount     numeric(14,2) NOT NULL CHECK (amount > 0),
    FOREIGN KEY (invoice_id, fin_year) REFERENCES invoice(invoice_id, fin_year)
);

-- GST rules ---------------------------------------------------------------

CREATE FUNCTION trg_line_sign() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    dt doc_type;
BEGIN
    SELECT doc_type INTO dt FROM invoice
    WHERE invoice_id = NEW.invoice_id AND fin_year = NEW.fin_year;
    IF dt = 'CRN' THEN
        IF NEW.taxable_value > 0 OR NEW.igst > 0 OR NEW.cgst > 0
           OR NEW.sgst > 0 OR NEW.cess > 0 THEN
            RAISE EXCEPTION 'credit note amounts must be negative';
        END IF;
    ELSE
        IF NEW.taxable_value < 0 OR NEW.igst < 0 OR NEW.cgst < 0
           OR NEW.sgst < 0 OR NEW.cess < 0 THEN
            RAISE EXCEPTION 'only credit notes may carry negative amounts';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION trg_line_tax_heads() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    inv invoice%ROWTYPE;
    supplier_state char(2);
BEGIN
    SELECT * INTO inv FROM invoice
    WHERE invoice_id = NEW.invoice_id AND fin_year = NEW.fin_year;
    IF inv.is_import THEN
        RETURN NEW;
    END IF;
    supplier_state := CASE inv.direction
        WHEN 'INWARD' THEN substr(inv.party_gstin, 1, 2)
        ELSE substr(inv.gstin, 1, 2) END;
    IF inv.place_of_supply = supplier_state THEN
        IF NEW.igst <> 0 THEN
            RAISE EXCEPTION 'tax heads do not match place of supply';
        END IF;
    ELSE
        IF NEW.cgst <> 0 OR NEW.sgst <> 0 THEN
            RAISE EXCEPTION 'tax heads do not match place of supply';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION trg_line_composition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    allowed boolean;
    dir direction;
BEGIN
    SELECT i.direction, t.itc_allowed INTO dir, allowed
    FROM invoice i
    JOIN gstin_reg r ON r.gstin = i.gstin
    JOIN taxpayer_type t ON t.code = r.taxpayer_type
    WHERE i.invoice_id = NEW.invoice_id AND i.fin_year = NEW.fin_year;
    IF dir = 'INWARD' AND NOT allowed AND NEW.itc_category = 'ELIGIBLE' THEN
        RAISE EXCEPTION 'composition dealers cannot claim ITC';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION trg_line_sac() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    ttype text;
    dir direction;
    svc boolean;
BEGIN
    SELECT i.direction, r.taxpayer_type INTO dir, ttype
    FROM invoice i
    JOIN gstin_reg r ON r.gstin = i.gstin
    WHERE i.invoice_id = NEW.invoice_id AND i.fin_year = NEW.fin_year;
    IF ttype = 'SERVICE' AND dir = 'OUTWARD' THEN
        SELECT is_service INTO svc FROM hsn_sac WHERE code = NEW.hsn_sac;
        IF NOT svc THEN
            RAISE EXCEPTION 'service providers must use SAC codes';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_line_sign BEFORE INSERT OR UPDATE ON invoice_line
    FOR EACH ROW EXECUTE FUNCTION trg_line_sign();
CREATE TRIGGER trg_line_tax_heads BEFORE INSERT OR UPDATE ON invoice_line
    FOR EACH ROW EXECUTE FUNCTION trg_line_tax_heads();
CREATE TRIGGER trg_line_composition BEFORE INSERT OR UPDATE ON invoice_line
    FOR EACH ROW EXECUTE FUNCTION trg_line_composition();
CREATE TRIGGER trg_line_sac BEFORE INSERT OR UPDATE ON invoice_line
    FOR EACH ROW EXECUTE FUNCTION trg_line_sac();

-- Views -------------------------------------------------------------------

CREATE VIEW v_invoice_total AS
SELECT invoice_id, fin_year,
       sum(taxable_value) AS taxable_value,
       sum(igst) AS igst, sum(cgst) AS cgst, sum(sgst) AS sgst, sum(cess) AS cess,
       sum(igst + cgst + sgst + cess) AS total_tax
FROM invoice_line
GROUP BY invoice_id, fin_year;

CREATE VIEW v_rate_warnings AS
SELECT l.invoice_id, l.line_id, l.hsn_sac, l.rate,
       rate_on(l.hsn_sac, i.invoice_date) AS expected_rate
FROM invoice_line l
JOIN invoice i ON i.invoice_id = l.invoice_id AND i.fin_year = l.fin_year
WHERE l.rate <> rate_on(l.hsn_sac, i.invoice_date);
