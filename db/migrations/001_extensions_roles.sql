-- Extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;

-- Roles are cluster-wide, so create them idempotently.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rc_partner') THEN
        CREATE ROLE rc_partner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rc_staff') THEN
        CREATE ROLE rc_staff NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rc_client') THEN
        CREATE ROLE rc_client NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rc_api') THEN
        CREATE ROLE rc_api NOLOGIN NOINHERIT;
    END IF;
END $$;

-- The API role may switch into any application role (SET ROLE) but inherits none.
GRANT rc_partner, rc_staff, rc_client TO rc_api;
