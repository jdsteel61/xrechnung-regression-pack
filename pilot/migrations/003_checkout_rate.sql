-- Per-client checkout rate limiting (flood control). Rows hold only a salted
-- hash of the edge client address and a timestamp; the app deletes rows older
-- than the window. Idempotent; apply as the PostgreSQL administrator.
BEGIN;
CREATE TABLE IF NOT EXISTS xreg.checkout_rate (
    client text NOT NULL,
    at double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS checkout_rate_client_at ON xreg.checkout_rate (client, at);
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xreg_api') THEN
        GRANT SELECT, INSERT, DELETE ON xreg.checkout_rate TO xreg_api;
    END IF;
END
$$;
INSERT INTO xreg.schema_migrations(version) VALUES (3)
ON CONFLICT (version) DO NOTHING;
COMMIT;