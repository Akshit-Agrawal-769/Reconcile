-- Reconcile, MariaDB 10.4 port: rules, procedures, triggers, views and roles.
-- Load after schema.sql. Every routine is wrapped in DELIMITER $$ ... $$ so the file
-- imports the same way in phpMyAdmin and in the mysql command-line client.
--
-- Who is acting: every procedure takes the app user's id as its first argument and
-- checks that user's role and clients itself (MariaDB has no row-level security, so
-- the procedures are the only door: app roles get EXECUTE, not table writes).

DELIMITER $$

-- ================================================================= shared rules
-- These three match the PostgreSQL build exactly; check.py compares them on the server.

CREATE FUNCTION gstin_checksum_ok(g VARCHAR(20)) RETURNS BOOLEAN
    DETERMINISTIC NO SQL
BEGIN
    DECLARE chars CHAR(36) DEFAULT '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ';
    DECLARE total INT DEFAULT 0;
    DECLARE i INT DEFAULT 1;
    DECLARE v INT;
    DECLARE p INT;
    IF g IS NULL OR CHAR_LENGTH(g) <> 15 THEN
        RETURN FALSE;
    END IF;
    WHILE i <= 14 DO
        SET v = LOCATE(BINARY SUBSTRING(g, i, 1), BINARY chars) - 1;
        IF v < 0 THEN
            RETURN FALSE;
        END IF;
        SET p = v * IF(i % 2 = 1, 1, 2);
        SET total = total + p DIV 36 + p % 36;
        SET i = i + 1;
    END WHILE;
    SET v = LOCATE(BINARY SUBSTRING(g, 15, 1), BINARY chars) - 1;
    RETURN v >= 0 AND v = (36 - total % 36) % 36;
END$$

-- Invoice number as used for matching: upper case, a financial-year segment such as
-- 26-27 or 2026-2027 removed (only when its years are consecutive), punctuation
-- removed, and leading zeros of each number dropped. "inv/0104/26-27" -> "INV104".
CREATE FUNCTION norm_doc_no(d VARCHAR(60)) RETURNS VARCHAR(60)
    DETERMINISTIC NO SQL
BEGIN
    DECLARE fy_seg VARCHAR(80) DEFAULT '(?:^|[^A-Z0-9])(?:20)?[0-9]{2}[-/](?:20)?[0-9]{2}(?=$|[^A-Z0-9])';
    DECLARE rest VARCHAR(60);
    DECLARE outp VARCHAR(60) DEFAULT '';
    DECLARE m VARCHAR(60);
    DECLARE body VARCHAR(60);
    DECLARE lead CHAR(1);
    DECLARE pos INT;
    DECLARE s VARCHAR(60);
    IF d IS NULL THEN
        RETURN NULL;
    END IF;
    SET rest = UPPER(d);
    seg: LOOP
        SET pos = REGEXP_INSTR(rest, fy_seg);
        IF pos = 0 THEN
            LEAVE seg;
        END IF;
        SET m = REGEXP_SUBSTR(rest, fy_seg);
        SET outp = CONCAT(outp, SUBSTRING(rest, 1, pos - 1));
        SET lead = LEFT(m, 1);
        SET body = IF(lead REGEXP '[0-9]', m, SUBSTRING(m, 2));
        IF (CAST(RIGHT(REGEXP_SUBSTR(body, '^[0-9]+'), 2) AS UNSIGNED) + 1) % 100
           = CAST(RIGHT(REGEXP_SUBSTR(body, '[0-9]+$'), 2) AS UNSIGNED) THEN
            SET outp = CONCAT(outp, IF(lead REGEXP '[0-9]', '', lead));
        ELSE
            SET outp = CONCAT(outp, m);
        END IF;
        SET rest = SUBSTRING(rest, pos + CHAR_LENGTH(m));
    END LOOP;
    SET s = REGEXP_REPLACE(CONCAT(outp, rest), '[^A-Z0-9]', '');
    SET s = REGEXP_REPLACE(s, '(^|[A-Z])0+([0-9])', '\\1\\2');
    RETURN IF(s = '', '0', s);
END$$

-- Indian financial year of a date: April 2026 to March 2027 is '2026-27'.
CREATE FUNCTION fin_year_of(d DATE) RETURNS CHAR(7)
    DETERMINISTIC NO SQL
BEGIN
    DECLARE y INT DEFAULT YEAR(d) - (MONTH(d) < 4);
    RETURN CONCAT(y, '-', LPAD(MOD(y + 1, 100), 2, '0'));
END$$

-- ================================================================= who may do what
CREATE FUNCTION user_role(p_user INT UNSIGNED) RETURNS VARCHAR(10)
    READS SQL DATA
BEGIN
    RETURN (SELECT role FROM app_user WHERE user_id = p_user);
END$$

-- Partner: every client of their firm. Staff: clients assigned to them. Client: itself.
CREATE FUNCTION can_see_client(p_user INT UNSIGNED, p_client INT UNSIGNED) RETURNS BOOLEAN
    READS SQL DATA
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM app_user u JOIN client c ON c.client_id = p_client
         WHERE u.user_id = p_user AND (
               (u.role = 'partner' AND u.firm_id = c.firm_id)
            OR (u.role = 'staff' AND EXISTS (SELECT 1 FROM staff_assignment a
                                              WHERE a.user_id = u.user_id AND a.client_id = c.client_id))
            OR (u.role = 'client' AND u.client_id = c.client_id)));
END$$

CREATE FUNCTION client_of_period(p_period INT UNSIGNED) RETURNS INT UNSIGNED
    READS SQL DATA
BEGIN
    RETURN (SELECT g.client_id FROM return_period p JOIN gstin_reg g ON g.gstin = p.gstin
             WHERE p.period_id = p_period);
END$$

-- Refuse unless the user may act on this period and the month is not closed.
CREATE PROCEDURE guard_period(IN p_user INT UNSIGNED, IN p_period INT UNSIGNED, IN p_allow_closed BOOLEAN)
    READS SQL DATA
BEGIN
    DECLARE v_state VARCHAR(20);
    IF client_of_period(p_period) IS NULL OR NOT can_see_client(p_user, client_of_period(p_period)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'not your client';
    END IF;
    SELECT state INTO v_state FROM return_period WHERE period_id = p_period;
    IF v_state = 'CLOSED' AND NOT p_allow_closed THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'period is closed';
    END IF;
END$$

CREATE PROCEDURE require_role(IN p_user INT UNSIGNED, IN p_roles VARCHAR(40))
    READS SQL DATA
BEGIN
    IF user_role(p_user) IS NULL OR FIND_IN_SET(user_role(p_user), p_roles) = 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'not allowed for your role';
    END IF;
END$$

-- Move a month to a new state. The trigger on return_period checks the move against
-- period_transition using @actor_role, which only this procedure sets.
CREATE PROCEDURE move_period(IN p_user INT UNSIGNED, IN p_period INT UNSIGNED, IN p_to VARCHAR(20))
    MODIFIES SQL DATA
BEGIN
    SET @actor_role = IFNULL(user_role(p_user), 'system');
    UPDATE return_period SET state = p_to WHERE period_id = p_period;
    SET @actor_role = NULL;
END$$

-- ================================================================= the month
CREATE PROCEDURE sp_open_period(IN p_user INT UNSIGNED, IN p_gstin CHAR(15), IN p_period CHAR(6),
                                OUT p_period_id INT UNSIGNED)
    MODIFIES SQL DATA
BEGIN
    DECLARE v_client INT UNSIGNED;
    CALL require_role(p_user, 'partner,staff');
    SELECT client_id INTO v_client FROM gstin_reg WHERE gstin = p_gstin;
    IF v_client IS NULL OR NOT can_see_client(p_user, v_client) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'not your client';
    END IF;
    SELECT period_id INTO p_period_id FROM return_period WHERE gstin = p_gstin AND period = p_period;
    IF p_period_id IS NULL THEN                      -- opening an open month is a no-op
        INSERT INTO return_period (gstin, period) VALUES (p_gstin, p_period);
        SET p_period_id = LAST_INSERT_ID();
        INSERT INTO audit_log (table_name, row_key, action, changed_by)
        VALUES ('return_period', p_period_id, 'OPEN', p_user);
    END IF;
END$$

CREATE PROCEDURE sp_submit_period(IN p_user INT UNSIGNED, IN p_period INT UNSIGNED)
    MODIFIES SQL DATA
BEGIN
    CALL require_role(p_user, 'partner,staff');
    CALL guard_period(p_user, p_period, FALSE);
    CALL move_period(p_user, p_period, 'AWAITING_APPROVAL');
END$$

CREATE PROCEDURE sp_client_approve(IN p_user INT UNSIGNED, IN p_period INT UNSIGNED)
    MODIFIES SQL DATA
BEGIN
    CALL require_role(p_user, 'client');
    CALL guard_period(p_user, p_period, FALSE);
    IF (SELECT state FROM return_period WHERE period_id = p_period) <> 'AWAITING_APPROVAL' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'month is not awaiting your approval';
    END IF;
    UPDATE return_period SET client_approved_at = NOW(), client_approved_by = p_user
     WHERE period_id = p_period;
    INSERT INTO audit_log (table_name, row_key, action, changed_by)
    VALUES ('return_period', p_period, 'CLIENT_APPROVE', p_user);
END$$

CREATE PROCEDURE sp_partner_override(IN p_user INT UNSIGNED, IN p_period INT UNSIGNED,
                                     IN p_reason VARCHAR(500))
    MODIFIES SQL DATA
BEGIN
    CALL require_role(p_user, 'partner');
    CALL guard_period(p_user, p_period, FALSE);
    IF p_reason IS NULL OR TRIM(p_reason) = '' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'override reason required';
    END IF;
    UPDATE return_period SET override_reason = TRIM(p_reason) WHERE period_id = p_period;
    INSERT INTO audit_log (table_name, row_key, action, changed_by, detail)
    VALUES ('return_period', p_period, 'OVERRIDE', p_user, TRIM(p_reason));
END$$

-- Reviews that belong to this month: decisions on invoices matched in its current import.
CREATE FUNCTION pending_reviews(p_period INT UNSIGNED) RETURNS INT
    READS SQL DATA
BEGIN
    RETURN (SELECT COUNT(*) FROM v_queue q WHERE q.period_id = p_period AND q.review_state = 'PROPOSED');
END$$

CREATE PROCEDURE sp_close_period(IN p_user INT UNSIGNED, IN p_period INT UNSIGNED)
    MODIFIES SQL DATA
BEGIN
    CALL require_role(p_user, 'partner');
    CALL guard_period(p_user, p_period, FALSE);
    IF (SELECT client_approved_at IS NULL AND override_reason IS NULL
          FROM return_period WHERE period_id = p_period) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'client approval missing';
    END IF;
    IF pending_reviews(p_period) > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'reviews awaiting approval';
    END IF;
    CALL move_period(p_user, p_period, 'CLOSED');
    UPDATE return_period SET closed_at = NOW() WHERE period_id = p_period;
END$$

CREATE PROCEDURE sp_reopen_period(IN p_user INT UNSIGNED, IN p_period INT UNSIGNED,
                                  IN p_reason VARCHAR(500))
    MODIFIES SQL DATA
BEGIN
    CALL require_role(p_user, 'partner');
    CALL guard_period(p_user, p_period, TRUE);
    IF p_reason IS NULL OR TRIM(p_reason) = '' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'reopen reason required';
    END IF;
    CALL move_period(p_user, p_period, 'IN_REVIEW');
    UPDATE return_period SET closed_at = NULL, client_approved_at = NULL, client_approved_by = NULL,
                             override_reason = NULL
     WHERE period_id = p_period;
    INSERT INTO audit_log (table_name, row_key, action, changed_by, detail)
    VALUES ('return_period', p_period, 'REOPEN', p_user, TRIM(p_reason));
END$$

-- ================================================================= loading data
-- Move this period's staged purchase-register rows into invoice and invoice_line.
-- Invoices already in the books (same supplier, type, number and financial year) are
-- skipped, so re-uploading an updated export of the month is safe.
CREATE PROCEDURE sp_import_books(IN p_user INT UNSIGNED, IN p_period INT UNSIGNED,
                                 OUT p_invoices INT)
    MODIFIES SQL DATA
BEGIN
    DECLARE v_gstin CHAR(15);
    DECLARE v_bad VARCHAR(60);
    DECLARE v_msg VARCHAR(200);
    DECLARE v_max BIGINT UNSIGNED;
    CALL require_role(p_user, 'partner,staff');
    CALL guard_period(p_user, p_period, FALSE);
    SELECT gstin INTO v_gstin FROM return_period WHERE period_id = p_period;

    SELECT gstin_of_supplier INTO v_bad FROM books_staging
     WHERE period_id = p_period AND NOT gstin_checksum_ok(gstin_of_supplier) LIMIT 1;
    IF v_bad IS NOT NULL THEN
        SET v_msg = CONCAT('invalid supplier GSTIN in books: ', v_bad);
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = v_msg;
    END IF;
    SELECT document_type INTO v_bad FROM books_staging
     WHERE period_id = p_period
       AND UPPER(TRIM(document_type)) NOT IN ('INVOICE', 'INV', 'CREDIT NOTE', 'CRN', 'DEBIT NOTE', 'DBN')
     LIMIT 1;
    IF v_bad IS NOT NULL THEN
        SET v_msg = CONCAT('unknown document type in books: ', v_bad);
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = v_msg;
    END IF;

    INSERT IGNORE INTO party (gstin, name)
    SELECT gstin_of_supplier, MAX(supplier_name) FROM books_staging
     WHERE period_id = p_period GROUP BY gstin_of_supplier;

    SELECT IFNULL(MAX(invoice_id), 0) INTO v_max FROM invoice;
    -- No INSERT IGNORE: that would also hide a bad date or a failed CHECK. Invoices already
    -- in the books are filtered out explicitly instead.
    INSERT INTO invoice (gstin, party_gstin, doc_type, invoice_no, invoice_date, pos, is_rcm, imported_by)
    SELECT DISTINCT v_gstin, s.gstin_of_supplier, t.doc_type,
           TRIM(s.invoice_number), STR_TO_DATE(s.invoice_date, '%d-%m-%Y'),
           LPAD(TRIM(s.place_of_supply), 2, '0'), UPPER(TRIM(s.reverse_charge)) IN ('Y', 'YES'), p_user
      FROM books_staging s
      JOIN (SELECT staging_id,
                   CASE UPPER(TRIM(document_type)) WHEN 'CREDIT NOTE' THEN 'CRN' WHEN 'CRN' THEN 'CRN'
                        WHEN 'DEBIT NOTE' THEN 'DBN' WHEN 'DBN' THEN 'DBN' ELSE 'INV' END AS doc_type
              FROM books_staging WHERE period_id = p_period) t ON t.staging_id = s.staging_id
     WHERE s.period_id = p_period
       AND NOT EXISTS (SELECT 1 FROM invoice x
                        WHERE x.gstin = v_gstin AND x.party_gstin = s.gstin_of_supplier
                          AND x.doc_type = t.doc_type
                          AND x.invoice_no_norm = norm_doc_no(TRIM(s.invoice_number))
                          AND x.fin_year = fin_year_of(STR_TO_DATE(s.invoice_date, '%d-%m-%Y')));
    SELECT COUNT(*) INTO p_invoices FROM invoice WHERE invoice_id > v_max;

    -- Lines only for invoices created just now (a skipped duplicate keeps its own lines).
    INSERT INTO invoice_line (invoice_id, line_no, hsn, rate, taxable_value, igst, cgst, sgst, cess, itc_category)
    SELECT i.invoice_id,
           ROW_NUMBER() OVER (PARTITION BY i.invoice_id ORDER BY s.staging_id),
           TRIM(s.hsn_sac), CAST(s.rate AS DECIMAL(5,2)),
           -- credit notes reduce credit, so they are stored negative (as in GSTR-2B)
           IF(i.doc_type = 'CRN', -1, 1) * CAST(s.taxable_value AS DECIMAL(14,2)),
           IF(i.doc_type = 'CRN', -1, 1) * CAST(IFNULL(NULLIF(s.integrated_tax_paid, ''), '0') AS DECIMAL(14,2)),
           IF(i.doc_type = 'CRN', -1, 1) * CAST(IFNULL(NULLIF(s.central_tax_paid, ''), '0') AS DECIMAL(14,2)),
           IF(i.doc_type = 'CRN', -1, 1) * CAST(IFNULL(NULLIF(s.state_ut_tax_paid, ''), '0') AS DECIMAL(14,2)),
           IF(i.doc_type = 'CRN', -1, 1) * CAST(IFNULL(NULLIF(s.cess_paid, ''), '0') AS DECIMAL(14,2)),
           CASE UPPER(TRIM(s.eligible_for_itc)) WHEN 'YES' THEN 'INPUTS'
                WHEN 'BLOCKED' THEN 'BLOCKED_17_5' ELSE 'INELIGIBLE' END
      FROM books_staging s
      JOIN invoice i ON i.gstin = v_gstin AND i.party_gstin = s.gstin_of_supplier
                    AND i.invoice_no = TRIM(s.invoice_number)
                    AND i.invoice_date = STR_TO_DATE(s.invoice_date, '%d-%m-%Y')
     WHERE s.period_id = p_period AND i.invoice_id > v_max;

    DELETE FROM books_staging WHERE period_id = p_period;
    INSERT INTO audit_log (table_name, row_key, action, changed_by, detail)
    VALUES ('return_period', p_period, 'IMPORT_BOOKS', p_user, CONCAT(p_invoices, ' invoices'));
END$$

-- A new GSTR-2B version for the period. Python reads the portal's JSON and then calls
-- sp_add_2b_entry once per document (MariaDB 10.4 has no JSON_TABLE).
CREATE PROCEDURE sp_begin_2b_import(IN p_user INT UNSIGNED, IN p_period INT UNSIGNED,
                                    IN p_source VARCHAR(20), OUT p_import_id INT UNSIGNED)
    MODIFIES SQL DATA
BEGIN
    DECLARE v_version SMALLINT UNSIGNED;
    CALL require_role(p_user, 'partner,staff');
    CALL guard_period(p_user, p_period, FALSE);
    SELECT IFNULL(MAX(version), 0) + 1 INTO v_version FROM gstr2b_import WHERE period_id = p_period;
    INSERT INTO gstr2b_import (period_id, version, source, imported_by)
    VALUES (p_period, v_version, p_source, p_user);
    SET p_import_id = LAST_INSERT_ID();
END$$

CREATE PROCEDURE sp_add_2b_entry(IN p_user INT UNSIGNED, IN p_import INT UNSIGNED,
                                 IN p_party_gstin CHAR(15), IN p_party_name VARCHAR(160),
                                 IN p_doc_type VARCHAR(3), IN p_invoice_no VARCHAR(30),
                                 IN p_date DATE, IN p_taxable DECIMAL(14,2), IN p_igst DECIMAL(14,2),
                                 IN p_cgst DECIMAL(14,2), IN p_sgst DECIMAL(14,2), IN p_cess DECIMAL(14,2))
    MODIFIES SQL DATA
BEGIN
    DECLARE v_period INT UNSIGNED;
    SELECT period_id INTO v_period FROM gstr2b_import WHERE import_id = p_import;
    IF v_period IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'unknown import';
    END IF;
    CALL guard_period(p_user, v_period, FALSE);
    INSERT IGNORE INTO party (gstin, name) VALUES (p_party_gstin, IFNULL(p_party_name, p_party_gstin));
    INSERT INTO gstr2b_entry (import_id, party_gstin, doc_type, invoice_no, invoice_date,
                              taxable_value, igst, cgst, sgst, cess)
    VALUES (p_import, p_party_gstin, p_doc_type, p_invoice_no, p_date,
            p_taxable, p_igst, p_cgst, p_sgst, p_cess);
END$$

-- ================================================================= matching
-- Six passes, in order. A row matched by an earlier pass is never offered to a later one.
--   0 DUPLICATE      the same document twice in one 2B (all but the first)
--   1 EXACT          same supplier, type, number and FY; same date; every tax head equal
--   2 ROUNDING       as EXACT, but dates within the client's window, heads within rounding_tol
--   3 MISMATCH       same supplier, type, number and FY; some head differs beyond tolerance
--   4 ONLY_IN_BOOKS  books invoice of the month left over
--   5 ONLY_IN_2B     2B document left over
CREATE PROCEDURE sp_reconcile(IN p_user INT UNSIGNED, IN p_import INT UNSIGNED, OUT p_rows INT)
    MODIFIES SQL DATA
BEGIN
    DECLARE v_period INT UNSIGNED;
    DECLARE v_gstin CHAR(15);
    DECLARE v_month CHAR(6);
    DECLARE v_type VARCHAR(12);
    DECLARE v_tol DECIMAL(14,2);
    DECLARE v_window INT;
    DECLARE v_start DATE;

    SELECT i.period_id, p.gstin, p.period INTO v_period, v_gstin, v_month
      FROM gstr2b_import i JOIN return_period p ON p.period_id = i.period_id
     WHERE i.import_id = p_import;
    IF v_period IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'unknown import';
    END IF;
    CALL require_role(p_user, 'partner,staff');
    CALL guard_period(p_user, v_period, FALSE);
    SELECT g.taxpayer_type, c.rounding_tol, c.date_window_days INTO v_type, v_tol, v_window
      FROM gstin_reg g JOIN client c ON c.client_id = g.client_id WHERE g.gstin = v_gstin;
    IF v_type = 'COMPOSITION' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'composition dealers cannot claim ITC';
    END IF;
    SET v_start = STR_TO_DATE(CONCAT('01', v_month), '%d%m%Y');

    DELETE FROM recon_result WHERE import_id = p_import;

    -- Books invoices of the month that can carry ITC, with their tax totals.
    DROP TEMPORARY TABLE IF EXISTS tmp_books;
    CREATE TEMPORARY TABLE tmp_books AS
    SELECT i.invoice_id, i.party_gstin, i.doc_type, i.invoice_no_norm, i.fin_year, i.invoice_date,
           SUM(l.igst) AS igst, SUM(l.cgst) AS cgst, SUM(l.sgst) AS sgst, SUM(l.cess) AS cess,
           SUM(l.igst + l.cgst + l.sgst + l.cess) AS total_tax
      FROM invoice i JOIN invoice_line l ON l.invoice_id = i.invoice_id
     WHERE i.gstin = v_gstin AND i.invoice_date >= v_start
       AND i.invoice_date < v_start + INTERVAL 1 MONTH AND NOT i.is_rcm
     GROUP BY i.invoice_id
    HAVING SUM(l.itc_category <> 'BLOCKED_17_5') > 0;

    -- 0 DUPLICATE
    INSERT INTO recon_result (import_id, entry_id, status)
    SELECT p_import, d.entry_id, 'DUPLICATE'
      FROM (SELECT entry_id, ROW_NUMBER() OVER (PARTITION BY party_gstin, doc_type, invoice_no_norm, fin_year
                                                ORDER BY entry_id) AS rn
              FROM gstr2b_entry WHERE import_id = p_import) d
     WHERE d.rn > 1;

    -- 1 EXACT
    INSERT IGNORE INTO recon_result (import_id, invoice_id, entry_id, status, diff_tax)
    SELECT p_import, x.invoice_id, x.entry_id, 'EXACT', 0
      FROM (SELECT b.invoice_id, e.entry_id,
                   ROW_NUMBER() OVER (PARTITION BY e.entry_id ORDER BY b.invoice_id) AS rn
              FROM tmp_books b
              JOIN gstr2b_entry e ON e.import_id = p_import AND e.party_gstin = b.party_gstin
                                 AND e.doc_type = b.doc_type AND e.invoice_no_norm = b.invoice_no_norm
                                 AND e.fin_year = b.fin_year AND e.invoice_date = b.invoice_date
                                 AND e.igst = b.igst AND e.cgst = b.cgst AND e.sgst = b.sgst
                                 AND e.cess = b.cess
             WHERE NOT EXISTS (SELECT 1 FROM recon_result r
                                WHERE r.import_id = p_import AND r.entry_id = e.entry_id)) x
     WHERE x.rn = 1;

    -- 2 ROUNDING
    INSERT IGNORE INTO recon_result (import_id, invoice_id, entry_id, status, diff_tax)
    SELECT p_import, x.invoice_id, x.entry_id, 'ROUNDING', x.diff
      FROM (SELECT b.invoice_id, e.entry_id, b.total_tax - (e.igst + e.cgst + e.sgst + e.cess) AS diff,
                   ROW_NUMBER() OVER (PARTITION BY e.entry_id ORDER BY b.invoice_id) AS rn
              FROM tmp_books b
              JOIN gstr2b_entry e ON e.import_id = p_import AND e.party_gstin = b.party_gstin
                                 AND e.doc_type = b.doc_type AND e.invoice_no_norm = b.invoice_no_norm
                                 AND e.fin_year = b.fin_year
                                 AND ABS(DATEDIFF(e.invoice_date, b.invoice_date)) <= v_window
                                 AND ABS(b.igst - e.igst) <= v_tol AND ABS(b.cgst - e.cgst) <= v_tol
                                 AND ABS(b.sgst - e.sgst) <= v_tol AND ABS(b.cess - e.cess) <= v_tol
             WHERE NOT EXISTS (SELECT 1 FROM recon_result r
                                WHERE r.import_id = p_import
                                  AND (r.entry_id = e.entry_id OR r.invoice_id = b.invoice_id))) x
     WHERE x.rn = 1;

    -- 3 MISMATCH
    INSERT IGNORE INTO recon_result (import_id, invoice_id, entry_id, status, diff_tax)
    SELECT p_import, x.invoice_id, x.entry_id, 'MISMATCH', x.diff
      FROM (SELECT b.invoice_id, e.entry_id, b.total_tax - (e.igst + e.cgst + e.sgst + e.cess) AS diff,
                   ROW_NUMBER() OVER (PARTITION BY e.entry_id ORDER BY b.invoice_id) AS rn
              FROM tmp_books b
              JOIN gstr2b_entry e ON e.import_id = p_import AND e.party_gstin = b.party_gstin
                                 AND e.doc_type = b.doc_type AND e.invoice_no_norm = b.invoice_no_norm
                                 AND e.fin_year = b.fin_year
                                 AND (ABS(b.igst - e.igst) > v_tol OR ABS(b.cgst - e.cgst) > v_tol
                                      OR ABS(b.sgst - e.sgst) > v_tol OR ABS(b.cess - e.cess) > v_tol)
             WHERE NOT EXISTS (SELECT 1 FROM recon_result r
                                WHERE r.import_id = p_import
                                  AND (r.entry_id = e.entry_id OR r.invoice_id = b.invoice_id))) x
     WHERE x.rn = 1;

    -- 4 ONLY_IN_BOOKS
    INSERT INTO recon_result (import_id, invoice_id, status, diff_tax)
    SELECT p_import, b.invoice_id, 'ONLY_IN_BOOKS', b.total_tax
      FROM tmp_books b
     WHERE NOT EXISTS (SELECT 1 FROM recon_result r
                        WHERE r.import_id = p_import AND r.invoice_id = b.invoice_id);

    -- 5 ONLY_IN_2B
    INSERT INTO recon_result (import_id, entry_id, status, diff_tax)
    SELECT p_import, e.entry_id, 'ONLY_IN_2B', -(e.igst + e.cgst + e.sgst + e.cess)
      FROM gstr2b_entry e
     WHERE e.import_id = p_import
       AND NOT EXISTS (SELECT 1 FROM recon_result r WHERE r.import_id = p_import AND r.entry_id = e.entry_id);

    DROP TEMPORARY TABLE IF EXISTS tmp_books;
    SELECT COUNT(*) INTO p_rows FROM recon_result WHERE import_id = p_import;
    IF (SELECT state FROM return_period WHERE period_id = v_period) = 'OPEN' THEN
        CALL move_period(NULL, v_period, 'IN_REVIEW');
    END IF;
END$$

-- ================================================================= reviews
-- Staff (or a partner) decide what to do about one match row. Small amounts are
-- approved at once; above the client's threshold a partner must approve (maker-checker).
CREATE PROCEDURE sp_propose(IN p_user INT UNSIGNED, IN p_result BIGINT UNSIGNED,
                            IN p_decision VARCHAR(12), IN p_ims VARCHAR(8),
                            IN p_claim_pct DECIMAL(5,2), IN p_remark VARCHAR(500),
                            OUT p_review_id BIGINT UNSIGNED)
    MODIFIES SQL DATA
BEGIN
    DECLARE v_period INT UNSIGNED;
    DECLARE v_gstin CHAR(15);
    DECLARE v_party CHAR(15);
    DECLARE v_type VARCHAR(3);
    DECLARE v_no VARCHAR(30);
    DECLARE v_fy CHAR(7);
    DECLARE v_risk DECIMAL(14,2);
    DECLARE v_threshold DECIMAL(14,2);
    CALL require_role(p_user, 'partner,staff');
    SELECT gi.period_id, p.gstin,
           COALESCE(i.party_gstin, e.party_gstin), COALESCE(i.doc_type, e.doc_type),
           COALESCE(i.invoice_no_norm, e.invoice_no_norm), COALESCE(i.fin_year, e.fin_year),
           ABS(IFNULL(r.diff_tax, 0))
      INTO v_period, v_gstin, v_party, v_type, v_no, v_fy, v_risk
      FROM recon_result r
      JOIN gstr2b_import gi ON gi.import_id = r.import_id
      JOIN return_period p ON p.period_id = gi.period_id
      LEFT JOIN invoice i ON i.invoice_id = r.invoice_id
      LEFT JOIN gstr2b_entry e ON e.entry_id = r.entry_id
     WHERE r.result_id = p_result;
    IF v_period IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'unknown match row';
    END IF;
    CALL guard_period(p_user, v_period, FALSE);
    SELECT c.approval_threshold INTO v_threshold
      FROM gstin_reg g JOIN client c ON c.client_id = g.client_id WHERE g.gstin = v_gstin;

    INSERT INTO review (gstin, party_gstin, doc_type, invoice_no_norm, fin_year, decision, ims_action,
                        claim_pct, remark, state, proposed_by)
    VALUES (v_gstin, v_party, v_type, v_no, v_fy, p_decision, p_ims, p_claim_pct, p_remark,
            IF(v_risk <= v_threshold, 'APPROVED', 'PROPOSED'), p_user)
    ON DUPLICATE KEY UPDATE
        review_id = LAST_INSERT_ID(review_id),
        decision = VALUES(decision), ims_action = VALUES(ims_action),
        claim_pct = VALUES(claim_pct), remark = VALUES(remark), state = VALUES(state),
        proposed_by = VALUES(proposed_by), approved_by = NULL, executed_on = NULL;
    SET p_review_id = LAST_INSERT_ID();
END$$

CREATE PROCEDURE sp_approve(IN p_user INT UNSIGNED, IN p_review BIGINT UNSIGNED)
    MODIFIES SQL DATA
BEGIN
    DECLARE v_state VARCHAR(10);
    DECLARE v_maker INT UNSIGNED;
    DECLARE v_client INT UNSIGNED;
    IF user_role(p_user) IS NULL OR user_role(p_user) <> 'partner' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'only a partner can approve';
    END IF;
    SELECT r.state, r.proposed_by, g.client_id INTO v_state, v_maker, v_client
      FROM review r JOIN gstin_reg g ON g.gstin = r.gstin WHERE r.review_id = p_review;
    IF v_client IS NULL OR NOT can_see_client(p_user, v_client) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'not your client';
    END IF;
    IF v_state <> 'PROPOSED' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'review is not awaiting approval';
    END IF;
    UPDATE review SET state = 'APPROVED', approved_by = p_user WHERE review_id = p_review;
END$$

-- What the dashboard reads, through the same tenancy check as every other call.
CREATE PROCEDURE sp_queue(IN p_user INT UNSIGNED, IN p_period INT UNSIGNED)
    READS SQL DATA
BEGIN
    CALL guard_period(p_user, p_period, TRUE);
    SELECT result_id, status, party_gstin, invoice_no, diff_tax, review_id, decision, review_state
      FROM v_queue WHERE period_id = p_period
     ORDER BY priority, ABS(IFNULL(diff_tax, 0)) DESC, invoice_no;
END$$

CREATE PROCEDURE sp_totals(IN p_user INT UNSIGNED, IN p_period INT UNSIGNED)
    READS SQL DATA
BEGIN
    CALL guard_period(p_user, p_period, TRUE);
    SELECT itc_books, itc_2b, itc_at_risk, match_rate_pct FROM v_period_totals WHERE period_id = p_period;
END$$

-- ================================================================= triggers
CREATE TRIGGER gstin_reg_check BEFORE INSERT ON gstin_reg FOR EACH ROW
BEGIN
    IF NOT gstin_checksum_ok(NEW.gstin) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invalid GSTIN';
    END IF;
    IF SUBSTRING(NEW.gstin, 3, 10) <> (SELECT pan FROM client WHERE client_id = NEW.client_id) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'GSTIN does not belong to this client (PAN differs)';
    END IF;
END$$

CREATE TRIGGER party_check BEFORE INSERT ON party FOR EACH ROW
BEGIN
    IF NOT gstin_checksum_ok(NEW.gstin) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invalid GSTIN';
    END IF;
END$$

-- A closed month is frozen: its invoices cannot be added, changed or removed.
CREATE FUNCTION month_closed(p_gstin CHAR(15), p_date DATE) RETURNS BOOLEAN
    READS SQL DATA
BEGIN
    RETURN EXISTS (SELECT 1 FROM return_period
                    WHERE gstin = p_gstin AND period = DATE_FORMAT(p_date, '%m%Y') AND state = 'CLOSED');
END$$

CREATE TRIGGER invoice_norm_ins BEFORE INSERT ON invoice FOR EACH ROW
BEGIN
    IF month_closed(NEW.gstin, NEW.invoice_date) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invoice of a closed period cannot change';
    END IF;
    SET NEW.invoice_no_norm = norm_doc_no(NEW.invoice_no);
END$$

CREATE TRIGGER invoice_norm_upd BEFORE UPDATE ON invoice FOR EACH ROW
BEGIN
    IF month_closed(OLD.gstin, OLD.invoice_date) OR month_closed(NEW.gstin, NEW.invoice_date) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invoice of a closed period cannot change';
    END IF;
    SET NEW.invoice_no_norm = norm_doc_no(NEW.invoice_no);
END$$

CREATE TRIGGER invoice_frozen_del BEFORE DELETE ON invoice FOR EACH ROW
BEGIN
    IF month_closed(OLD.gstin, OLD.invoice_date) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invoice of a closed period cannot change';
    END IF;
END$$

CREATE TRIGGER invoice_line_frozen BEFORE UPDATE ON invoice_line FOR EACH ROW
BEGIN
    IF (SELECT month_closed(gstin, invoice_date) FROM invoice WHERE invoice_id = OLD.invoice_id) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invoice of a closed period cannot change';
    END IF;
END$$

CREATE TRIGGER gstr2b_entry_norm BEFORE INSERT ON gstr2b_entry FOR EACH ROW
BEGIN
    SET NEW.invoice_no_norm = norm_doc_no(NEW.invoice_no);
END$$

-- The month's state machine: only moves listed in period_transition, by the right role.
CREATE TRIGGER return_period_state BEFORE UPDATE ON return_period FOR EACH ROW
BEGIN
    IF NEW.state <> OLD.state AND NOT EXISTS (
        SELECT 1 FROM period_transition t
         WHERE t.from_state = OLD.state AND t.to_state = NEW.state
           AND (t.actor_role = @actor_role
                OR (@actor_role = 'partner' AND t.actor_role IN ('staff', 'system')))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'illegal state transition';
    END IF;
END$$

CREATE TRIGGER return_period_audit AFTER UPDATE ON return_period FOR EACH ROW
BEGIN
    IF NEW.state <> OLD.state THEN
        INSERT INTO audit_log (table_name, row_key, action, detail)
        VALUES ('return_period', NEW.period_id, 'STATE', CONCAT(OLD.state, ' -> ', NEW.state));
    END IF;
END$$

-- Maker-checker: whoever proposed a decision cannot also approve it.
CREATE TRIGGER review_maker_checker BEFORE UPDATE ON review FOR EACH ROW
BEGIN
    IF NEW.approved_by IS NOT NULL AND NEW.approved_by = NEW.proposed_by THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'maker cannot approve own review';
    END IF;
END$$

CREATE TRIGGER review_audit_ins AFTER INSERT ON review FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, row_key, action, changed_by, detail)
    VALUES ('review', NEW.review_id, CONCAT('PROPOSE ', NEW.decision), NEW.proposed_by, NEW.state);
END$$

CREATE TRIGGER review_audit_upd AFTER UPDATE ON review FOR EACH ROW
BEGIN
    INSERT INTO audit_log (table_name, row_key, action, changed_by, detail)
    VALUES ('review', NEW.review_id, CONCAT(OLD.state, ' -> ', NEW.state),
            IFNULL(NEW.approved_by, NEW.proposed_by), NEW.decision);
END$$

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit log is append-only';
END$$

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit log is append-only';
END$$

DELIMITER ;

-- ================================================================= views
CREATE OR REPLACE VIEW v_current_import AS
SELECT i.* FROM gstr2b_import i
 WHERE i.version = (SELECT MAX(j.version) FROM gstr2b_import j WHERE j.period_id = i.period_id);

CREATE OR REPLACE VIEW v_invoice_total AS
SELECT l.invoice_id, SUM(l.taxable_value) AS taxable_value,
       SUM(l.igst + l.cgst + l.sgst + l.cess) AS total_tax,
       SUM(CASE WHEN l.itc_category IN ('INELIGIBLE', 'BLOCKED_17_5') THEN 0
                ELSE l.igst + l.cgst + l.sgst + l.cess END) AS eligible_tax
  FROM invoice_line l GROUP BY l.invoice_id;

-- The exception queue: every match row of the month's current 2B, with any decision.
CREATE OR REPLACE VIEW v_queue AS
SELECT ci.period_id, r.result_id, r.status,
       COALESCE(i.party_gstin, e.party_gstin) AS party_gstin,
       COALESCE(i.invoice_no, e.invoice_no) AS invoice_no,
       r.diff_tax, v.review_id, v.decision, v.state AS review_state,
       CASE WHEN r.status IN ('EXACT', 'ROUNDING') THEN 1 ELSE 0 END AS priority
  FROM recon_result r
  JOIN v_current_import ci ON ci.import_id = r.import_id
  JOIN return_period p ON p.period_id = ci.period_id
  LEFT JOIN invoice i ON i.invoice_id = r.invoice_id
  LEFT JOIN gstr2b_entry e ON e.entry_id = r.entry_id
  LEFT JOIN review v ON v.gstin = p.gstin
                    AND v.party_gstin = COALESCE(i.party_gstin, e.party_gstin)
                    AND v.doc_type = COALESCE(i.doc_type, e.doc_type)
                    AND v.invoice_no_norm = COALESCE(i.invoice_no_norm, e.invoice_no_norm)
                    AND v.fin_year = COALESCE(i.fin_year, e.fin_year);

-- The four numbers on the period dashboard.
CREATE OR REPLACE VIEW v_period_totals AS
SELECT ci.period_id,
       IFNULL(SUM(CASE WHEN r.invoice_id IS NOT NULL THEN t.total_tax END), 0) AS itc_books,
       IFNULL(SUM(CASE WHEN r.entry_id IS NOT NULL AND r.status <> 'DUPLICATE'
                       THEN e.igst + e.cgst + e.sgst + e.cess END), 0) AS itc_2b,
       IFNULL(SUM(CASE WHEN r.status IN ('MISMATCH', 'ONLY_IN_BOOKS') THEN ABS(r.diff_tax) END), 0)
           AS itc_at_risk,
       ROUND(100 * SUM(r.status IN ('EXACT', 'ROUNDING'))
                 / NULLIF(SUM(r.invoice_id IS NOT NULL), 0), 1) AS match_rate_pct
  FROM v_current_import ci
  JOIN recon_result r ON r.import_id = ci.import_id
  LEFT JOIN v_invoice_total t ON t.invoice_id = r.invoice_id
  LEFT JOIN gstr2b_entry e ON e.entry_id = r.entry_id
 GROUP BY ci.period_id;

-- ================================================================= reference data
INSERT INTO period_transition (from_state, to_state, actor_role) VALUES
    ('OPEN', 'IN_REVIEW', 'system'),
    ('IN_REVIEW', 'AWAITING_APPROVAL', 'staff'),
    ('AWAITING_APPROVAL', 'IN_REVIEW', 'staff'),
    ('AWAITING_APPROVAL', 'CLOSED', 'partner'),
    ('CLOSED', 'IN_REVIEW', 'partner');

-- ================================================================= roles
-- Application roles get EXECUTE on procedures and SELECT on views, never table writes.
CREATE ROLE IF NOT EXISTS rc_partner;
CREATE ROLE IF NOT EXISTS rc_staff;
CREATE ROLE IF NOT EXISTS rc_client;
-- The views have no tenancy filter, so app roles read them only through sp_queue and
-- sp_totals, which check the user first.
GRANT EXECUTE ON PROCEDURE sp_queue TO rc_partner, rc_staff, rc_client;
GRANT EXECUTE ON PROCEDURE sp_totals TO rc_partner, rc_staff, rc_client;
GRANT EXECUTE ON PROCEDURE sp_open_period TO rc_partner, rc_staff;
GRANT EXECUTE ON PROCEDURE sp_import_books TO rc_partner, rc_staff;
GRANT EXECUTE ON PROCEDURE sp_begin_2b_import TO rc_partner, rc_staff;
GRANT EXECUTE ON PROCEDURE sp_add_2b_entry TO rc_partner, rc_staff;
GRANT EXECUTE ON PROCEDURE sp_reconcile TO rc_partner, rc_staff;
GRANT EXECUTE ON PROCEDURE sp_propose TO rc_partner, rc_staff;
GRANT EXECUTE ON PROCEDURE sp_submit_period TO rc_partner, rc_staff;
GRANT EXECUTE ON PROCEDURE sp_approve TO rc_partner;
GRANT EXECUTE ON PROCEDURE sp_close_period TO rc_partner;
GRANT EXECUTE ON PROCEDURE sp_reopen_period TO rc_partner;
GRANT EXECUTE ON PROCEDURE sp_partner_override TO rc_partner;
GRANT EXECUTE ON PROCEDURE sp_client_approve TO rc_client;
GRANT INSERT ON books_staging TO rc_partner, rc_staff;
