-- Demo seed: one firm, partner, two staff, one client user, three clients.
-- Run against a migrated database: psql -v ON_ERROR_STOP=1 -f db/seed/demo.sql
BEGIN;

INSERT INTO firm(name) VALUES ('Demo CA Associates');

INSERT INTO client(firm_id, name, pan) VALUES
    ((SELECT firm_id FROM firm WHERE name = 'Demo CA Associates'), 'Acme Traders (Regular)',      'AAPFU0939F'),
    ((SELECT firm_id FROM firm WHERE name = 'Demo CA Associates'), 'Bharat Bakers (Composition)', 'BBQPK1234L'),
    ((SELECT firm_id FROM firm WHERE name = 'Demo CA Associates'), 'Cyan Consulting (Service)',   'CCRSM5678Q');

INSERT INTO gstin_reg(gstin, client_id, taxpayer_type, filing_freq, state_code, reg_date) VALUES
    ('07AAPFU0939F1ZX', (SELECT client_id FROM client WHERE pan = 'AAPFU0939F'), 'REGULAR',     'MONTHLY', '07', '2019-04-01'),
    ('07BBQPK1234L1ZF', (SELECT client_id FROM client WHERE pan = 'BBQPK1234L'), 'COMPOSITION', 'QRMP',    '07', '2019-04-01'),
    ('07CCRSM5678Q1Z5', (SELECT client_id FROM client WHERE pan = 'CCRSM5678Q'), 'SERVICE',     'MONTHLY', '07', '2019-04-01');

INSERT INTO app_user(firm_id, client_id, email, role, password_hash)
SELECT f.firm_id, c.client_id, u.email, u.role::user_role, crypt('demo123', gen_salt('bf'))
FROM firm f
CROSS JOIN (VALUES
    ('partner@demo.test', 'partner', NULL),
    ('staff.a@demo.test', 'staff',   NULL),
    ('staff.b@demo.test', 'staff',   NULL),
    ('client1@demo.test', 'client',  'AAPFU0939F')
) AS u(email, role, pan)
LEFT JOIN client c ON c.pan = u.pan
WHERE f.name = 'Demo CA Associates';

INSERT INTO staff_assignment(user_id, client_id)
SELECT u.user_id, c.client_id
FROM (VALUES
    ('staff.a@demo.test', 'AAPFU0939F'),
    ('staff.a@demo.test', 'BBQPK1234L'),
    ('staff.b@demo.test', 'CCRSM5678Q')
) AS a(email, pan)
JOIN app_user u ON u.email = a.email
JOIN client c ON c.pan = a.pan;

COMMIT;
