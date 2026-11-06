-- Reconcile: GST input-tax-credit reconciliation, MariaDB 10.4 port (XAMPP).
--
-- Load order: schema.sql, then logic.sql, then seed.sql (phpMyAdmin "Import" tab, or
-- `python xampp/reconcile_cli.py init`). The full build runs on PostgreSQL 16 with
-- row-level security and partitioning; this port keeps the data model and the rules a
-- reader of the report can check by hand.
--
-- Every table below is in BCNF once its derived columns are set aside: invoice_no_norm
-- and fin_year are computed from another column of the same row (by a trigger or the
-- column definition), kept for indexing, and never written by users.

CREATE DATABASE IF NOT EXISTS reconcile CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE reconcile;

-- ---------------------------------------------------------------- tenancy
CREATE TABLE firm (
    firm_id  INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name     VARCHAR(120) NOT NULL,
    PRIMARY KEY (firm_id)
) ENGINE=InnoDB;

CREATE TABLE client (
    client_id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    firm_id            INT UNSIGNED NOT NULL,
    name               VARCHAR(120) NOT NULL,
    pan                CHAR(10) NOT NULL,
    approval_threshold DECIMAL(14,2) NOT NULL DEFAULT 5000.00,  -- above this a partner must approve
    rounding_tol       DECIMAL(14,2) NOT NULL DEFAULT 1.00,     -- rupees per tax head
    date_window_days   TINYINT UNSIGNED NOT NULL DEFAULT 10,  -- same as the PostgreSQL build
    PRIMARY KEY (client_id),
    UNIQUE KEY client_pan (pan),
    CONSTRAINT client_firm FOREIGN KEY (firm_id) REFERENCES firm (firm_id),
    CONSTRAINT client_pan_format CHECK (pan REGEXP '^[A-Z]{5}[0-9]{4}[A-Z]$'),
    CONSTRAINT client_tol CHECK (rounding_tol BETWEEN 0 AND 10)
) ENGINE=InnoDB;

-- A client can hold one GSTIN per state. State and PAN are read off the GSTIN itself
-- (LEFT(gstin, 2), SUBSTRING(gstin, 3, 10)), so they are not stored.
CREATE TABLE gstin_reg (
    gstin          CHAR(15) NOT NULL,
    client_id      INT UNSIGNED NOT NULL,
    taxpayer_type  ENUM('REGULAR', 'COMPOSITION', 'SERVICE', 'SEZ') NOT NULL DEFAULT 'REGULAR',
    reg_date       DATE NOT NULL,
    PRIMARY KEY (gstin),
    CONSTRAINT gstin_reg_client FOREIGN KEY (client_id) REFERENCES client (client_id)
) ENGINE=InnoDB;

-- Suppliers, shared by every client of the firm that buys from them.
CREATE TABLE party (
    gstin  CHAR(15) NOT NULL,
    name   VARCHAR(160) NOT NULL,
    PRIMARY KEY (gstin)
) ENGINE=InnoDB;

CREATE TABLE app_user (
    user_id        INT UNSIGNED NOT NULL AUTO_INCREMENT,
    firm_id        INT UNSIGNED NOT NULL,
    client_id      INT UNSIGNED NULL,            -- set only for client logins
    email          VARCHAR(160) NOT NULL,
    role           ENUM('partner', 'staff', 'client') NOT NULL,
    password_hash  CHAR(64) NOT NULL,           -- SHA2(salt || password, 256); demo only
    PRIMARY KEY (user_id),
    UNIQUE KEY app_user_email (email),
    CONSTRAINT app_user_firm FOREIGN KEY (firm_id) REFERENCES firm (firm_id),
    CONSTRAINT app_user_client FOREIGN KEY (client_id) REFERENCES client (client_id),
    CONSTRAINT app_user_client_role CHECK ((role = 'client') = (client_id IS NOT NULL))
) ENGINE=InnoDB;

CREATE TABLE staff_assignment (
    user_id    INT UNSIGNED NOT NULL,
    client_id  INT UNSIGNED NOT NULL,
    PRIMARY KEY (user_id, client_id),
    CONSTRAINT staff_assignment_user FOREIGN KEY (user_id) REFERENCES app_user (user_id),
    CONSTRAINT staff_assignment_client FOREIGN KEY (client_id) REFERENCES client (client_id)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------- the month
-- Allowed moves of the month's state machine, and who may make each one.
CREATE TABLE period_transition (
    from_state  ENUM('OPEN', 'IN_REVIEW', 'AWAITING_APPROVAL', 'CLOSED') NOT NULL,
    to_state    ENUM('OPEN', 'IN_REVIEW', 'AWAITING_APPROVAL', 'CLOSED') NOT NULL,
    actor_role  ENUM('partner', 'staff', 'client', 'system') NOT NULL,
    PRIMARY KEY (from_state, to_state)
) ENGINE=InnoDB;

CREATE TABLE return_period (
    period_id           INT UNSIGNED NOT NULL AUTO_INCREMENT,
    gstin               CHAR(15) NOT NULL,
    period              CHAR(6) NOT NULL,                 -- MMYYYY, as on the portal
    state               ENUM('OPEN', 'IN_REVIEW', 'AWAITING_APPROVAL', 'CLOSED')
                        NOT NULL DEFAULT 'OPEN',
    client_approved_at  DATETIME NULL,
    client_approved_by  INT UNSIGNED NULL,
    override_reason     VARCHAR(500) NULL,
    closed_at           DATETIME NULL,
    PRIMARY KEY (period_id),
    UNIQUE KEY return_period_once (gstin, period),
    CONSTRAINT return_period_gstin FOREIGN KEY (gstin) REFERENCES gstin_reg (gstin),
    CONSTRAINT return_period_approver FOREIGN KEY (client_approved_by) REFERENCES app_user (user_id),
    CONSTRAINT return_period_format CHECK (period REGEXP '^(0[1-9]|1[0-2])20[0-9]{2}$')
) ENGINE=InnoDB;

-- Due dates for monthly filers depend only on the return and the month, so they live
-- here, once. (Quarterly QRMP filers have state-wise dates; this port covers monthly filers.)
CREATE TABLE return_calendar (
    return_type  ENUM('GSTR1', 'GSTR3B') NOT NULL,
    period       CHAR(6) NOT NULL,
    due_date     DATE NOT NULL,
    PRIMARY KEY (return_type, period)
) ENGINE=InnoDB;

CREATE TABLE filing (
    gstin        CHAR(15) NOT NULL,
    return_type  ENUM('GSTR1', 'GSTR3B') NOT NULL,
    period       CHAR(6) NOT NULL,
    filed_on     DATE NULL,
    late_fee     DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    PRIMARY KEY (gstin, return_type, period),
    CONSTRAINT filing_gstin FOREIGN KEY (gstin) REFERENCES gstin_reg (gstin),
    CONSTRAINT filing_calendar FOREIGN KEY (return_type, period)
        REFERENCES return_calendar (return_type, period)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------- books side
CREATE TABLE invoice (
    invoice_id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    gstin            CHAR(15) NOT NULL,                  -- our client's registration
    party_gstin      CHAR(15) NOT NULL,                  -- the supplier
    doc_type         ENUM('INV', 'CRN', 'DBN') NOT NULL,
    invoice_no       VARCHAR(30) NOT NULL,
    invoice_date     DATE NOT NULL,
    pos              CHAR(2) NOT NULL,                   -- place of supply (state code)
    is_rcm           BOOLEAN NOT NULL DEFAULT FALSE,
    imported_by      INT UNSIGNED NOT NULL,
    -- Derived, never written by users: kept for the uniqueness rule below.
    invoice_no_norm  VARCHAR(30) NOT NULL DEFAULT '',     -- set by trigger invoice_norm_*
    fin_year         CHAR(7) AS (CONCAT(YEAR(invoice_date) - (MONTH(invoice_date) < 4), '-',
                                        LPAD(MOD(YEAR(invoice_date) - (MONTH(invoice_date) < 4) + 1, 100), 2, '0')))
                     PERSISTENT,
    PRIMARY KEY (invoice_id),
    UNIQUE KEY invoice_once (gstin, party_gstin, doc_type, invoice_no_norm, fin_year),
    KEY invoice_month (gstin, invoice_date),
    CONSTRAINT invoice_gstin FOREIGN KEY (gstin) REFERENCES gstin_reg (gstin),
    CONSTRAINT invoice_party FOREIGN KEY (party_gstin) REFERENCES party (gstin),
    CONSTRAINT invoice_importer FOREIGN KEY (imported_by) REFERENCES app_user (user_id),
    CONSTRAINT invoice_pos CHECK (pos REGEXP '^[0-9]{2}$')
) ENGINE=InnoDB;

CREATE TABLE invoice_line (
    invoice_id     BIGINT UNSIGNED NOT NULL,
    line_no        SMALLINT UNSIGNED NOT NULL,
    hsn            VARCHAR(8) NOT NULL,
    rate           DECIMAL(5,2) NOT NULL,
    taxable_value  DECIMAL(14,2) NOT NULL,
    igst           DECIMAL(14,2) NOT NULL DEFAULT 0,
    cgst           DECIMAL(14,2) NOT NULL DEFAULT 0,
    sgst           DECIMAL(14,2) NOT NULL DEFAULT 0,
    cess           DECIMAL(14,2) NOT NULL DEFAULT 0,
    itc_category   ENUM('INPUTS', 'CAPITAL_GOODS', 'INPUT_SERVICES', 'INELIGIBLE', 'BLOCKED_17_5')
                   NOT NULL DEFAULT 'INPUTS',
    PRIMARY KEY (invoice_id, line_no),
    CONSTRAINT invoice_line_invoice FOREIGN KEY (invoice_id) REFERENCES invoice (invoice_id)
        ON DELETE CASCADE,
    -- GST rates in force for 2026-27, including 40% (from 22 September 2025).
    CONSTRAINT invoice_line_rate CHECK (rate IN (0, 0.1, 0.25, 1.5, 3, 5, 6, 12, 18, 28, 40)),
    -- A supply is either inter-state (IGST) or intra-state (CGST + SGST), never both.
    -- CGST and SGST are half each, but rounding can leave them a paisa apart, so they
    -- are not forced equal (that would also make sgst a function of cgst).
    CONSTRAINT invoice_line_heads CHECK (igst = 0 OR (cgst = 0 AND sgst = 0))
) ENGINE=InnoDB;

-- The purchase register exactly as the CSV arrives, before validation. It is a load
-- buffer, not part of the data model: sp_import_books moves rows into invoice and
-- invoice_line and empties it.
CREATE TABLE books_staging (
    staging_id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    period_id            INT UNSIGNED NOT NULL,
    gstin_of_supplier    VARCHAR(20),
    supplier_name        VARCHAR(160),
    document_type        VARCHAR(20),
    invoice_number       VARCHAR(30),
    invoice_date         VARCHAR(10),          -- DD-MM-YYYY text, parsed on import
    place_of_supply      VARCHAR(4),
    reverse_charge       VARCHAR(3),
    hsn_sac              VARCHAR(8),
    rate                 VARCHAR(8),
    taxable_value        VARCHAR(20),
    integrated_tax_paid  VARCHAR(20),
    central_tax_paid     VARCHAR(20),
    state_ut_tax_paid    VARCHAR(20),
    cess_paid            VARCHAR(20),
    eligible_for_itc     VARCHAR(12),
    PRIMARY KEY (staging_id),
    KEY books_staging_period (period_id),
    CONSTRAINT books_staging_period FOREIGN KEY (period_id) REFERENCES return_period (period_id)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------- portal side
-- Each upload of GSTR-2B is a new version; the latest version is the current one
-- (computed in v_current_import, not stored).
CREATE TABLE gstr2b_import (
    import_id    INT UNSIGNED NOT NULL AUTO_INCREMENT,
    period_id    INT UNSIGNED NOT NULL,
    version      SMALLINT UNSIGNED NOT NULL,
    source       ENUM('DRAFT_2B', 'IMS', 'RECOMPUTED_2B') NOT NULL,
    imported_by  INT UNSIGNED NOT NULL,
    imported_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (import_id),
    UNIQUE KEY gstr2b_import_version (period_id, version),
    CONSTRAINT gstr2b_import_period FOREIGN KEY (period_id) REFERENCES return_period (period_id),
    CONSTRAINT gstr2b_import_user FOREIGN KEY (imported_by) REFERENCES app_user (user_id)
) ENGINE=InnoDB;

CREATE TABLE gstr2b_entry (
    entry_id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    import_id        INT UNSIGNED NOT NULL,
    party_gstin      CHAR(15) NOT NULL,
    doc_type         ENUM('INV', 'CRN', 'DBN') NOT NULL,
    invoice_no       VARCHAR(30) NOT NULL,
    invoice_date     DATE NOT NULL,
    taxable_value    DECIMAL(14,2) NOT NULL,
    igst             DECIMAL(14,2) NOT NULL DEFAULT 0,
    cgst             DECIMAL(14,2) NOT NULL DEFAULT 0,
    sgst             DECIMAL(14,2) NOT NULL DEFAULT 0,
    cess             DECIMAL(14,2) NOT NULL DEFAULT 0,
    invoice_no_norm  VARCHAR(30) NOT NULL DEFAULT '',     -- set by trigger, like invoice
    fin_year         CHAR(7) AS (CONCAT(YEAR(invoice_date) - (MONTH(invoice_date) < 4), '-',
                                        LPAD(MOD(YEAR(invoice_date) - (MONTH(invoice_date) < 4) + 1, 100), 2, '0')))
                     PERSISTENT,
    PRIMARY KEY (entry_id),
    KEY gstr2b_entry_key (import_id, party_gstin, doc_type, invoice_no_norm, fin_year),
    CONSTRAINT gstr2b_entry_import FOREIGN KEY (import_id) REFERENCES gstr2b_import (import_id)
        ON DELETE CASCADE,
    CONSTRAINT gstr2b_entry_party FOREIGN KEY (party_gstin) REFERENCES party (gstin)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------- matching
-- One row per books invoice and per 2B document of an import. A matched pair has both
-- ids; "only in books" has no entry, "only in 2B" and "duplicate" have no invoice.
CREATE TABLE recon_result (
    result_id   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    import_id   INT UNSIGNED NOT NULL,
    invoice_id  BIGINT UNSIGNED NULL,
    entry_id    BIGINT UNSIGNED NULL,
    status      ENUM('EXACT', 'ROUNDING', 'MISMATCH', 'ONLY_IN_BOOKS', 'ONLY_IN_2B', 'DUPLICATE')
                NOT NULL,
    diff_tax    DECIMAL(14,2) NULL,                -- books tax minus 2B tax
    PRIMARY KEY (result_id),
    UNIQUE KEY recon_result_invoice_once (import_id, invoice_id),
    UNIQUE KEY recon_result_entry_once (import_id, entry_id),
    CONSTRAINT recon_result_import FOREIGN KEY (import_id) REFERENCES gstr2b_import (import_id)
        ON DELETE CASCADE,
    CONSTRAINT recon_result_invoice FOREIGN KEY (invoice_id) REFERENCES invoice (invoice_id),
    CONSTRAINT recon_result_entry FOREIGN KEY (entry_id) REFERENCES gstr2b_entry (entry_id)
        ON DELETE CASCADE,
    CONSTRAINT recon_result_shape CHECK (
        (status IN ('EXACT', 'ROUNDING', 'MISMATCH') AND invoice_id IS NOT NULL AND entry_id IS NOT NULL)
     OR (status = 'ONLY_IN_BOOKS' AND invoice_id IS NOT NULL AND entry_id IS NULL)
     OR (status IN ('ONLY_IN_2B', 'DUPLICATE') AND invoice_id IS NULL AND entry_id IS NOT NULL))
) ENGINE=InnoDB;

-- A decision about a real invoice, keyed by the invoice itself (not by a match row), so
-- it survives a new 2B version and carries forward to later months.
CREATE TABLE review (
    review_id        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    gstin            CHAR(15) NOT NULL,
    party_gstin      CHAR(15) NOT NULL,
    doc_type         ENUM('INV', 'CRN', 'DBN') NOT NULL,
    invoice_no_norm  VARCHAR(30) NOT NULL,
    fin_year         CHAR(7) NOT NULL,
    decision         ENUM('ACCEPT', 'CHASE', 'DEFER', 'PARTIAL', 'INELIGIBLE') NOT NULL,
    ims_action       ENUM('ACCEPT', 'REJECT', 'PENDING', 'NONE') NOT NULL,
    claim_pct        DECIMAL(5,2) NULL,
    remark           VARCHAR(500) NULL,
    state            ENUM('PROPOSED', 'APPROVED', 'DONE') NOT NULL DEFAULT 'PROPOSED',
    proposed_by      INT UNSIGNED NOT NULL,
    approved_by      INT UNSIGNED NULL,
    executed_on      DATE NULL,
    PRIMARY KEY (review_id),
    UNIQUE KEY review_invoice (gstin, party_gstin, doc_type, invoice_no_norm, fin_year),
    CONSTRAINT review_gstin FOREIGN KEY (gstin) REFERENCES gstin_reg (gstin),
    CONSTRAINT review_party FOREIGN KEY (party_gstin) REFERENCES party (gstin),
    CONSTRAINT review_maker FOREIGN KEY (proposed_by) REFERENCES app_user (user_id),
    CONSTRAINT review_checker FOREIGN KEY (approved_by) REFERENCES app_user (user_id),
    CONSTRAINT review_partial CHECK ((decision = 'PARTIAL') = (claim_pct IS NOT NULL)),
    CONSTRAINT review_pct CHECK (claim_pct IS NULL OR claim_pct BETWEEN 0 AND 100),
    CONSTRAINT review_done CHECK ((state = 'DONE') = (executed_on IS NOT NULL))
) ENGINE=InnoDB;

-- Chasing a supplier who has not filed. The client's GSTIN comes from the period.
CREATE TABLE followup (
    followup_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    period_id      INT UNSIGNED NOT NULL,
    party_gstin    CHAR(15) NOT NULL,
    channel        ENUM('EMAIL', 'WHATSAPP', 'CALL', 'LETTER') NOT NULL,
    sent_by        INT UNSIGNED NOT NULL,
    sent_on        DATE NULL,
    response       VARCHAR(1000) NULL,
    next_chase_on  DATE NULL,
    status         ENUM('OPEN', 'SENT', 'RESPONDED', 'RESOLVED') NOT NULL DEFAULT 'OPEN',
    PRIMARY KEY (followup_id),
    CONSTRAINT followup_period FOREIGN KEY (period_id) REFERENCES return_period (period_id),
    CONSTRAINT followup_party FOREIGN KEY (party_gstin) REFERENCES party (gstin),
    CONSTRAINT followup_user FOREIGN KEY (sent_by) REFERENCES app_user (user_id)
) ENGINE=InnoDB;

-- Append-only: triggers write here; nobody may update or delete (see logic.sql).
CREATE TABLE audit_log (
    audit_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    table_name  VARCHAR(40) NOT NULL,
    row_key     VARCHAR(80) NOT NULL,
    action      VARCHAR(40) NOT NULL,
    changed_by  INT UNSIGNED NULL,
    changed_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    detail      VARCHAR(1000) NULL,
    PRIMARY KEY (audit_id)
) ENGINE=InnoDB;
