# Reconcile

## Database setup

```bash
docker compose up -d db
pip install -r requirements-dev.txt
cp .env.example .env   # then: export DATABASE_URL=postgresql://reconcile:reconcile@localhost:5432/reconcile
python db/migrate.py --url $DATABASE_URL
pytest db/tests
```
