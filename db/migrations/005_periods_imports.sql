-- 005_periods_imports.sql: return periods, versioned GSTR-2B imports, books staging/import.

CREATE TYPE period_state AS ENUM ('OPEN', 'IN_REVIEW', 'AWAITING_APPROVAL', 'CLOSED');

CREATE TABLE return_period (
    period_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    gstin              gstin NOT NULL REFERENCES gstin_reg(gstin),
    period             char(6) NOT NULL CHECK (period ~ '^(0[1-9]|1[0-2])[0-9]{4}$'),
    fin_year           text NOT NULL,
    state              period_state NOT NULL DEFAULT 'OPEN',
    client_approved_by bigint REFERENCES app_user(user_id),
    client_approved_at timestamptz,
    override_reason    text,
    closed_by          bigint REFERENCES app_user(user_id),
    closed_at          timestamptz,
    UNIQUE (gstin, period),
    CHECK (fin_year = fin_year_of(period_start(period)))
);

CREATE TABLE gstr2b_import (
    import_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    period_id   bigint NOT NULL REFERENCES return_period(period_id),
    version     int NOT NULL,
    source      text NOT NULL CHECK (source IN ('IMS', 'DRAFT_2B', 'RECOMPUTED_2B')),
    is_current  boolean NOT NULL DEFAULT true,
    payload     jsonb NOT NULL,
    imported_by bigint NOT NULL REFERENCES app_user(user_id),
    imported_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (period_id, version)
);
CREATE UNIQUE INDEX gstr2b_import_current_uq ON gstr2b_import (period_id) WHERE is_current;

-- fin_year = the period's FY (set by sp_import_gstr2b); plain column, see R2.
CREATE TABLE gstr2b_entry (
    entry_id        bigint GENERATED ALWAYS AS IDENTITY,
    import_id       bigint NOT NULL REFERENCES gstr2b_import(import_id),
    fin_year        text NOT NULL,
    party_gstin     char(15) NOT NULL,
    gstin_valid     boolean NOT NULL,
    party_name      text,
    section         text NOT NULL CHECK (section IN ('b2b', 'b2ba', 'cdnr')),
    doc_type        doc_type NOT NULL,
    invoice_no      text NOT NULL,
    invoice_no_norm text GENERATED ALWAYS AS (norm_doc_no(invoice_no)) STORED,
    invoice_date    date NOT NULL,
    taxable_value   numeric(14,2) NOT NULL,
    igst            numeric(14,2) NOT NULL DEFAULT 0,
    cgst            numeric(14,2) NOT NULL DEFAULT 0,
    sgst            numeric(14,2) NOT NULL DEFAULT 0,
    cess            numeric(14,2) NOT NULL DEFAULT 0,
    itc_available   boolean NOT NULL DEFAULT true,
    orig_invoice_no text,
    ims_status      text NOT NULL DEFAULT 'NO_ACTION',
    PRIMARY KEY (entry_id, fin_year)
) PARTITION BY LIST (fin_year);

CREATE TABLE gstr2b_entry_2025_26 PARTITION OF gstr2b_entry FOR VALUES IN ('2025-26');
CREATE TABLE gstr2b_entry_2026_27 PARTITION OF gstr2b_entry FOR VALUES IN ('2026-27');
CREATE TABLE gstr2b_entry_2027_28 PARTITION OF gstr2b_entry FOR VALUES IN ('2027-28');
CREATE TABLE gstr2b_entry_default PARTITION OF gstr2b_entry DEFAULT;

CREATE INDEX gstr2b_entry_import_idx ON gstr2b_entry (import_id);
CREATE INDEX gstr2b_entry_party_idx ON gstr2b_entry (party_gstin, invoice_no_norm);

CREATE TABLE books_staging (
    staging_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    period_id          bigint NOT NULL REFERENCES return_period(period_id),
    gstin_of_supplier  text,
    supplier_name      text,
    document_type      text,
    invoice_number     text,
    invoice_date       text,
    place_of_supply    text,
    reverse_charge     text,
    hsn_sac            text,
    rate               text,
    taxable_value      text,
    integrated_tax_paid text,
    central_tax_paid   text,
    state_ut_tax_paid  text,
    cess_paid          text,
    eligible_for_itc   text
);

CREATE FUNCTION sp_open_period(p_gstin text, p_period char(6)) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v_id bigint;
BEGIN
    INSERT INTO return_period(gstin, period, fin_year)
    VALUES (p_gstin, p_period, fin_year_of(period_start(p_period)))
    ON CONFLICT (gstin, period) DO NOTHING
    RETURNING period_id INTO v_id;
    IF v_id IS NULL THEN
        SELECT period_id INTO v_id FROM return_period
        WHERE gstin = p_gstin AND period = p_period;
    END IF;
    RETURN v_id;
END;
$$;

-- Amount of one head for a 2B document: sum of items[] when present, else the document field.
CREATE FUNCTION gstr2b_amt(doc jsonb, head text) RETURNS numeric
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN jsonb_typeof(doc->'items') = 'array' AND jsonb_array_length(doc->'items') > 0
        THEN (SELECT COALESCE(sum((i->>head)::numeric), 0) FROM jsonb_array_elements(doc->'items') i)
        ELSE COALESCE((doc->>head)::numeric, 0)
    END
$$;

CREATE FUNCTION sp_import_gstr2b(p_period_id bigint, p_payload jsonb, p_source text, p_user_id bigint)
RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v_period return_period%ROWTYPE;
    v_version int;
    v_import bigint;
    v_dd jsonb := p_payload->'data'->'docdata';
BEGIN
    IF p_source NOT IN ('IMS', 'DRAFT_2B', 'RECOMPUTED_2B') THEN
        RAISE EXCEPTION 'invalid source: %', p_source;
    END IF;
    SELECT * INTO v_period FROM return_period WHERE period_id = p_period_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown period %', p_period_id;
    END IF;
    IF p_payload->'data'->>'rtnprd' IS DISTINCT FROM v_period.period THEN
        RAISE EXCEPTION 'rtnprd mismatch: payload % vs period %',
            p_payload->'data'->>'rtnprd', v_period.period;
    END IF;

    SELECT COALESCE(max(version), 0) + 1 INTO v_version
    FROM gstr2b_import WHERE period_id = p_period_id;
    UPDATE gstr2b_import SET is_current = false WHERE period_id = p_period_id AND is_current;
    INSERT INTO gstr2b_import(period_id, version, source, is_current, payload, imported_by)
    VALUES (p_period_id, v_version, p_source, true, p_payload, p_user_id)
    RETURNING import_id INTO v_import;

    -- b2b and b2ba invoices
    INSERT INTO gstr2b_entry(import_id, fin_year, party_gstin, gstin_valid, party_name, section,
        doc_type, invoice_no, invoice_date, taxable_value, igst, cgst, sgst, cess,
        itc_available, orig_invoice_no)
    SELECT v_import, v_period.fin_year, s.c->>'ctin', gstin_checksum_ok(s.c->>'ctin'),
        s.c->>'trdnm', s.sec, 'INV', d->>'inum', to_date(d->>'dt', 'DD-MM-YYYY'),
        gstr2b_amt(d, 'txval'), gstr2b_amt(d, 'igst'), gstr2b_amt(d, 'cgst'),
        gstr2b_amt(d, 'sgst'), gstr2b_amt(d, 'cess'),
        COALESCE(upper(d->>'itcavl'), 'Y') <> 'N',
        CASE WHEN s.sec = 'b2ba' THEN d->>'oinum' END
    FROM (
        SELECT 'b2b' AS sec, c FROM jsonb_array_elements(COALESCE(v_dd->'b2b', '[]'::jsonb)) c
        UNION ALL
        SELECT 'b2ba', c FROM jsonb_array_elements(COALESCE(v_dd->'b2ba', '[]'::jsonb)) c
    ) s
    CROSS JOIN LATERAL jsonb_array_elements(COALESCE(s.c->'inv', '[]'::jsonb)) d;

    -- credit / debit notes: credit notes are stored negated
    INSERT INTO gstr2b_entry(import_id, fin_year, party_gstin, gstin_valid, party_name, section,
        doc_type, invoice_no, invoice_date, taxable_value, igst, cgst, sgst, cess, itc_available)
    SELECT v_import, v_period.fin_year, c->>'ctin', gstin_checksum_ok(c->>'ctin'),
        c->>'trdnm', 'cdnr',
        CASE WHEN upper(d->>'ntty') = 'C' THEN 'CRN'::doc_type ELSE 'DBN'::doc_type END,
        d->>'ntnum', to_date(d->>'dt', 'DD-MM-YYYY'),
        sg.m * gstr2b_amt(d, 'txval'), sg.m * gstr2b_amt(d, 'igst'), sg.m * gstr2b_amt(d, 'cgst'),
        sg.m * gstr2b_amt(d, 'sgst'), sg.m * gstr2b_amt(d, 'cess'),
        COALESCE(upper(d->>'itcavl'), 'Y') <> 'N'
    FROM jsonb_array_elements(COALESCE(v_dd->'cdnr', '[]'::jsonb)) c
    CROSS JOIN LATERAL jsonb_array_elements(COALESCE(c->'nt', '[]'::jsonb)) d
    CROSS JOIN LATERAL (SELECT CASE WHEN upper(d->>'ntty') = 'C' THEN -1 ELSE 1 END AS m) sg;

    -- only checksum-valid suppliers become party rows
    INSERT INTO party(gstin, name)
    SELECT DISTINCT ON (party_gstin) party_gstin, COALESCE(party_name, party_gstin)
    FROM gstr2b_entry
    WHERE import_id = v_import AND fin_year = v_period.fin_year AND gstin_valid
    ORDER BY party_gstin, entry_id
    ON CONFLICT (gstin) DO NOTHING;

    UPDATE return_period SET state = 'IN_REVIEW'
    WHERE period_id = p_period_id AND state = 'OPEN';

    RETURN v_import;
END;
$$;

CREATE FUNCTION sp_import_books(p_period_id bigint, p_user_id bigint) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE
    v_period return_period%ROWTYPE;
    r record;
    v_dt doc_type;
    v_date date;
    v_fy text;
    v_inv bigint;
    v_sign int;
    v_count int := 0;
BEGIN
    SELECT * INTO v_period FROM return_period WHERE period_id = p_period_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown period %', p_period_id;
    END IF;

    FOR r IN
        SELECT gstin_of_supplier AS sup, max(supplier_name) AS sname, document_type AS dtype,
               invoice_number AS inum, invoice_date AS idate,
               max(place_of_supply) AS pos, bool_or(upper(reverse_charge) = 'Y') AS rcm
        FROM books_staging WHERE period_id = p_period_id
        GROUP BY 1, 3, 4, 5
        ORDER BY min(staging_id)
    LOOP
        IF NOT gstin_checksum_ok(r.sup) THEN
            RAISE EXCEPTION 'invalid supplier GSTIN in books: %', r.sup;
        END IF;
        v_dt := CASE upper(r.dtype)
            WHEN 'INVOICE' THEN 'INV' WHEN 'INV' THEN 'INV'
            WHEN 'CREDIT NOTE' THEN 'CRN' WHEN 'CRN' THEN 'CRN'
            WHEN 'DEBIT NOTE' THEN 'DBN' WHEN 'DBN' THEN 'DBN'
            ELSE NULL END;
        IF v_dt IS NULL THEN
            RAISE EXCEPTION 'invalid document type in books: %', r.dtype;
        END IF;
        v_sign := CASE WHEN v_dt = 'CRN' THEN -1 ELSE 1 END;
        v_date := to_date(r.idate, 'DD-MM-YYYY');
        v_fy := fin_year_of(v_date);

        INSERT INTO party(gstin, name) VALUES (r.sup, COALESCE(r.sname, r.sup))
        ON CONFLICT (gstin) DO NOTHING;

        INSERT INTO invoice(gstin, party_gstin, direction, doc_type, invoice_no,
                            invoice_date, fin_year, place_of_supply, is_rcm)
        VALUES (v_period.gstin, r.sup, 'INWARD', v_dt, r.inum, v_date, v_fy, r.pos, r.rcm)
        RETURNING invoice_id INTO v_inv;

        INSERT INTO invoice_line(invoice_id, fin_year, hsn_sac, taxable_value, rate,
                                 igst, cgst, sgst, cess, itc_category)
        SELECT v_inv, v_fy, hsn_sac, v_sign * taxable_value::numeric, rate::numeric,
               v_sign * COALESCE(NULLIF(integrated_tax_paid, '')::numeric, 0),
               v_sign * COALESCE(NULLIF(central_tax_paid, '')::numeric, 0),
               v_sign * COALESCE(NULLIF(state_ut_tax_paid, '')::numeric, 0),
               v_sign * COALESCE(NULLIF(cess_paid, '')::numeric, 0),
               CASE lower(eligible_for_itc)
                   WHEN 'yes' THEN 'ELIGIBLE'
                   WHEN 'no' THEN 'INELIGIBLE'
                   WHEN 'ineligible' THEN 'INELIGIBLE'
                   WHEN 'blocked' THEN 'BLOCKED_17_5'
                   ELSE NULL END::itc_category
        FROM books_staging
        WHERE period_id = p_period_id AND gstin_of_supplier = r.sup
          AND document_type = r.dtype AND invoice_number = r.inum AND invoice_date = r.idate
        ORDER BY staging_id;
        v_count := v_count + 1;
    END LOOP;

    DELETE FROM books_staging WHERE period_id = p_period_id;
    RETURN v_count;
END;
$$;
