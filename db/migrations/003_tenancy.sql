-- 003_tenancy.sql: firm, users, clients, GSTIN registrations, parties, helper functions.

CREATE FUNCTION norm_doc_no(d text) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE
    rest text := upper(d COLLATE "C");
    outp text := '';
    pos  int;
    m    text[];
    s    text;
BEGIN
    -- Remove a financial-year segment (e.g. 26-27, 2026-27, 2026-2027) only when
    -- it is delimiter-bounded and its years are consecutive.
    LOOP
        pos := NULL;
        m := regexp_match(rest,
            '((?:^|[^A-Z0-9])((?:20)?[0-9]{2})[-/]((?:20)?[0-9]{2}))(?=$|[^A-Z0-9])');
        EXIT WHEN m IS NULL;
        pos := strpos(rest, m[1]);
        -- strpos finds the first occurrence, which is the regex match (leftmost).
        outp := outp || substr(rest, 1, pos - 1);
        IF ((right(m[2], 2))::int + 1) % 100 = (right(m[3], 2))::int THEN
            s := substr(m[1], 1, 1);            -- keep the leading delimiter
            IF s ~ '[0-9]' THEN s := ''; END IF;  -- (match began at string start)
            outp := outp || s;
        ELSE
            outp := outp || m[1];
        END IF;
        rest := substr(rest, pos + length(m[1]));
    END LOOP;
    outp := outp || rest;
    s := regexp_replace(
             regexp_replace(outp, '[^A-Z0-9]', '', 'g'),
             '(^|[A-Z])0+([0-9])', '\1\2', 'g');
    RETURN CASE WHEN s = '' THEN '0' ELSE s END;
END;
$$;

CREATE FUNCTION fin_year_of(d date) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT y || '-' || to_char((y + 1) % 100, 'FM00')
    FROM (
        SELECT CASE WHEN date_part('month', d) >= 4
                    THEN date_part('year', d)::int
                    ELSE date_part('year', d)::int - 1 END AS y
    ) t
$$;

CREATE FUNCTION period_start(p char(6)) RETURNS date
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT make_date(substr(p, 3, 4)::int, substr(p, 1, 2)::int, 1)
$$;

CREATE TYPE user_role AS ENUM ('partner', 'staff', 'client');

CREATE TABLE firm (
    firm_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name    text NOT NULL
);

CREATE TABLE client (
    client_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    firm_id            bigint NOT NULL REFERENCES firm(firm_id),
    name               text NOT NULL,
    pan                char(10) NOT NULL UNIQUE,
    rounding_tol       numeric(14,2) NOT NULL DEFAULT 1.00,
    review_tol         numeric(14,2) NOT NULL DEFAULT 10.00,
    approval_threshold numeric(14,2) NOT NULL DEFAULT 5000.00,
    date_window_days   int NOT NULL DEFAULT 10,
    fuzzy_max_dist     int NOT NULL DEFAULT 2,
    r37_interest_pct   numeric(5,2) NOT NULL DEFAULT 18.00,
    created_at         timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN client.rounding_tol IS 'Matching tolerance; verify on gst.gov.in';
COMMENT ON COLUMN client.approval_threshold IS 'Firm policy threshold for partner approval';
COMMENT ON COLUMN client.r37_interest_pct IS 'Rule 37 reversal interest rate; verify on gst.gov.in';

CREATE TABLE app_user (
    user_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    firm_id       bigint NOT NULL REFERENCES firm(firm_id),
    client_id     bigint REFERENCES client(client_id),
    email         text NOT NULL UNIQUE,
    role          user_role NOT NULL,
    password_hash text NOT NULL,
    CHECK ((role = 'client') = (client_id IS NOT NULL))
);

CREATE TABLE staff_assignment (
    user_id   bigint NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
    client_id bigint NOT NULL REFERENCES client(client_id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, client_id)
);

CREATE TABLE gstin_reg (
    gstin         gstin PRIMARY KEY,
    client_id     bigint NOT NULL REFERENCES client(client_id),
    taxpayer_type text NOT NULL REFERENCES taxpayer_type(code),
    filing_freq   text NOT NULL CHECK (filing_freq IN ('MONTHLY','QRMP')),
    state_code    char(2) NOT NULL,
    reg_date      date NOT NULL,
    CHECK (state_code = substr(gstin, 1, 2))
);

CREATE FUNCTION trg_gstin_pan() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF substr(NEW.gstin, 3, 10) IS DISTINCT FROM
       (SELECT pan FROM client WHERE client_id = NEW.client_id) THEN
        RAISE EXCEPTION 'PAN mismatch: GSTIN % does not belong to client PAN', NEW.gstin;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_gstin_pan BEFORE INSERT OR UPDATE ON gstin_reg
    FOR EACH ROW EXECUTE FUNCTION trg_gstin_pan();

CREATE TABLE party (
    gstin       gstin PRIMARY KEY,
    name        text NOT NULL,
    filing_freq text CHECK (filing_freq IN ('MONTHLY','QRMP'))
);
