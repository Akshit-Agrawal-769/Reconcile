-- 002_reference.sql: GSTIN domain, taxpayer types, HSN/SAC, dated tax rates, filing rules.

CREATE FUNCTION gstin_checksum_ok(g text) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE
    chars constant text := '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ';
    total int := 0;
    v int;
    p int;
    i int;
BEGIN
    IF length(g) <> 15 THEN
        RETURN false;
    END IF;
    FOR i IN 1..14 LOOP
        v := position(substr(g, i, 1) IN chars) - 1;
        IF v < 0 THEN
            RETURN false;
        END IF;
        p := v * CASE WHEN i % 2 = 1 THEN 1 ELSE 2 END;
        total := total + p / 36 + p % 36;
    END LOOP;
    v := position(substr(g, 15, 1) IN chars) - 1;
    RETURN v >= 0 AND v = (36 - total % 36) % 36;
END;
$$;

CREATE DOMAIN gstin AS char(15)
    CONSTRAINT gstin_format CHECK (VALUE ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$')
    CONSTRAINT gstin_checksum CHECK (gstin_checksum_ok(VALUE::text));

CREATE TABLE taxpayer_type (
    code        text PRIMARY KEY,
    itc_allowed boolean NOT NULL,
    description text
);

INSERT INTO taxpayer_type(code, itc_allowed, description) VALUES
    ('REGULAR',     true,  'Regular taxpayer'),
    ('COMPOSITION', false, 'Composition scheme taxpayer (no ITC)'),
    ('SERVICE',     true,  'Service provider');

CREATE TABLE hsn_sac (
    code        text PRIMARY KEY,
    description text,
    is_service  boolean NOT NULL
);

CREATE TABLE tax_rate (
    code           text NOT NULL REFERENCES hsn_sac(code),
    effective_from date NOT NULL,
    effective_to   date,
    rate           numeric(5,2) NOT NULL,
    PRIMARY KEY (code, effective_from),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);
COMMENT ON TABLE tax_rate IS 'GST rates by validity period; verify on gst.gov.in';

CREATE FUNCTION rate_on(p_code text, p_date date) RETURNS numeric
LANGUAGE sql STABLE AS $$
    SELECT rate FROM tax_rate
    WHERE code = p_code
      AND effective_from <= p_date
      AND (effective_to IS NULL OR p_date <= effective_to)
    ORDER BY effective_from DESC
    LIMIT 1
$$;

CREATE TABLE filing_rule (
    taxpayer_type text NOT NULL REFERENCES taxpayer_type(code),
    filing_freq   text NOT NULL CHECK (filing_freq IN ('MONTHLY','QRMP')),
    return_type   text NOT NULL,
    frequency     text NOT NULL CHECK (frequency IN ('MONTHLY','QUARTERLY','ANNUAL')),
    due_day       int  NOT NULL,
    PRIMARY KEY (taxpayer_type, filing_freq, return_type)
);
COMMENT ON TABLE filing_rule IS 'Due days are indicative; verify on gst.gov.in';

INSERT INTO filing_rule(taxpayer_type, filing_freq, return_type, frequency, due_day)
SELECT t.code, r.ff, r.rt, r.fq, r.dd
FROM (VALUES ('REGULAR'), ('SERVICE')) AS t(code)
CROSS JOIN (VALUES
    ('MONTHLY','GSTR1',  'MONTHLY',   11),
    ('MONTHLY','GSTR3B', 'MONTHLY',   20),
    ('QRMP',   'IFF',    'MONTHLY',   13),
    ('QRMP',   'GSTR3B', 'QUARTERLY', 22)
) AS r(ff, rt, fq, dd);

INSERT INTO filing_rule(taxpayer_type, filing_freq, return_type, frequency, due_day) VALUES
    ('COMPOSITION','QRMP','CMP08','QUARTERLY',18),
    ('COMPOSITION','QRMP','GSTR4','ANNUAL',30);

-- SAMPLE DATA: illustrative HSN/SAC codes and rates, not authoritative.
INSERT INTO hsn_sac(code, description, is_service) VALUES
    ('8471',   'Computers and peripherals (sample)', false),
    ('7308',   'Iron/steel structures (sample)',     false),
    ('998314', 'IT consulting services (sample)',    true),
    ('996511', 'Goods transport services (sample)',  true);

INSERT INTO tax_rate(code, effective_from, effective_to, rate) VALUES
    ('8471', '2017-07-01', '2025-09-21', 18.00),
    ('8471', '2025-09-22', NULL,         18.00);
