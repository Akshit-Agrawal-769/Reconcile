-- Demo data for the MariaDB port: one CA firm, three clients, four logins.
-- Every password is demo123. The hash is SHA2-256 of 'reconcile:<email>:<password>',
-- good enough for a lab demo, not for production (the PostgreSQL build uses bcrypt).

INSERT INTO firm (name) VALUES ('Demo CA Associates');

INSERT INTO client (firm_id, name, pan) VALUES
    (1, 'Acme Traders (Regular)', 'AAPFU0939F'),
    (1, 'Bharat Bakers (Composition)', 'BBQPK1234L'),
    (1, 'Cyan Consulting (Service)', 'CCRSM5678Q');

INSERT INTO gstin_reg (gstin, client_id, taxpayer_type, reg_date) VALUES
    ('07AAPFU0939F1ZX', 1, 'REGULAR', '2019-04-01'),
    ('07BBQPK1234L1ZF', 2, 'COMPOSITION', '2019-04-01'),
    ('07CCRSM5678Q1Z5', 3, 'SERVICE', '2019-04-01');

INSERT INTO app_user (firm_id, client_id, email, role, password_hash) VALUES
    (1, NULL, 'partner@demo.test', 'partner', SHA2('reconcile:partner@demo.test:demo123', 256)),
    (1, NULL, 'staff.a@demo.test', 'staff', SHA2('reconcile:staff.a@demo.test:demo123', 256)),
    (1, NULL, 'staff.b@demo.test', 'staff', SHA2('reconcile:staff.b@demo.test:demo123', 256)),
    (1, 1, 'client1@demo.test', 'client', SHA2('reconcile:client1@demo.test:demo123', 256));

INSERT INTO staff_assignment (user_id, client_id) VALUES (2, 1), (2, 2), (3, 3);

-- Due dates for 2026-27 (monthly filers): GSTR-1 on the 11th and GSTR-3B on the 20th
-- of the following month.
INSERT INTO return_calendar (return_type, period, due_date) VALUES
    ('GSTR1', '042026', '2026-05-11'), ('GSTR3B', '042026', '2026-05-20'),
    ('GSTR1', '052026', '2026-06-11'), ('GSTR3B', '052026', '2026-06-20'),
    ('GSTR1', '062026', '2026-07-11'), ('GSTR3B', '062026', '2026-07-20'),
    ('GSTR1', '072026', '2026-08-11'), ('GSTR3B', '072026', '2026-08-20'),
    ('GSTR1', '082026', '2026-09-11'), ('GSTR3B', '082026', '2026-09-20'),
    ('GSTR1', '092026', '2026-10-11'), ('GSTR3B', '092026', '2026-10-20'),
    ('GSTR1', '102026', '2026-11-11'), ('GSTR3B', '102026', '2026-11-20'),
    ('GSTR1', '112026', '2026-12-11'), ('GSTR3B', '112026', '2026-12-20'),
    ('GSTR1', '122026', '2027-01-11'), ('GSTR3B', '122026', '2027-01-20'),
    ('GSTR1', '012027', '2027-02-11'), ('GSTR3B', '012027', '2027-02-20'),
    ('GSTR1', '022027', '2027-03-11'), ('GSTR3B', '022027', '2027-03-20'),
    ('GSTR1', '032027', '2027-04-11'), ('GSTR3B', '032027', '2027-04-20');
