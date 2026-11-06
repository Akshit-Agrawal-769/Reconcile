# Reconcile on XAMPP (MariaDB + Python)

This folder is the version of Reconcile the lab report describes: the data model,
the rules and the matcher in MariaDB 10.4 (what XAMPP 8 ships), driven from Python.
The full build in the rest of the repo runs on PostgreSQL 16 and adds row-level
security, partitioning, three more matching passes and the web app.

## 1. Start MySQL

Open the XAMPP Control Panel and press **Start** next to **MySQL**. The defaults this
folder assumes are host `127.0.0.1`, port `3306`, user `root`, no password. If yours
differ, pass `--host --port --user --password` to the commands below.

## 2. Load the database

Either way works.

- **phpMyAdmin:** open http://localhost/phpmyadmin, go to **Import**, and import
  `schema.sql`, then `logic.sql`, then `seed.sql`, in that order. This creates a
  database called `reconcile`.
- **Python:** from the repository root,

  ```
  pip install mysql-connector-python
  python xampp/reconcile_cli.py init
  ```

## 3. Check that everything works

```
python xampp/check.py
```

This makes a separate database `reconcile_check`, runs one synthetic month whose
right answers are known, and tries every rule: a staff member reaching another
firm's client, a partner approving their own decision, closing a month before the
client approves, editing a closed month, deleting the audit log. Each line says
`[ok]` or `[FAIL]`; the last line should be `ALL CHECKS PASSED`.

## 4. Work a month by hand

```
python tools/synth.py --clients 1 --docs 60 --period 092026 --out data/xampp
python xampp/reconcile_cli.py load-month --as staff.a@demo.test --gstin <GSTIN from data/xampp/clients.json> \
       --period 092026 --books data/xampp/books_<GSTIN>.csv --gstr2b data/xampp/gstr2b_<GSTIN>.json
python xampp/reconcile_cli.py queue  --as staff.a@demo.test --period-id 1
python xampp/reconcile_cli.py totals --as staff.a@demo.test --period-id 1
python xampp/reconcile_cli.py decide --as staff.a@demo.test --result 12 --decision CHASE --ims PENDING
python xampp/reconcile_cli.py approve --as partner@demo.test --review 1
python xampp/reconcile_cli.py submit --as staff.a@demo.test --period-id 1
```

The synthetic client has to exist first; `check.py` shows how it is registered
(a `client`, its `gstin_reg` row, and a `staff_assignment`).

Logins in the demo data, all with password `demo123`: `partner@demo.test`,
`staff.a@demo.test` (Acme and Bharat), `staff.b@demo.test` (Cyan), `client1@demo.test`.

## Files

| File | What it holds |
|---|---|
| `schema.sql` | 19 tables with keys, foreign keys and CHECK constraints |
| `logic.sql` | functions, guards, stored procedures (import, matching, reviews, month workflow), triggers, views, roles |
| `seed.sql` | the demo firm, clients, logins and the 2026-27 due-date calendar |
| `reconcile_cli.py` | the Python command line |
| `check.py` | the end-to-end check |
| `rules.py` | Python copies of the GSTIN check, invoice-number normalisation and financial year |
