-- Append-only gate transition audit: every close/reopen records actor, reason
-- and evidence (Service._set_gate / Service.verify_gate). The application role
-- receives SELECT+INSERT only, so running code cannot rewrite audit history.
-- Idempotent; apply as the PostgreSQL administrator.
BEGIN;
CREATE TABLE IF NOT EXISTS xreg.gate_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at double precision,
    gate text NOT NULL,
    action text NOT NULL CHECK (action IN ('closed', 'reopened', 'verification_failed')),
    actor text NOT NULL,
    reason text NOT NULL,
    evidence text
);
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xreg_api') THEN
        GRANT SELECT, INSERT ON xreg.gate_audit TO xreg_api;
    END IF;
END
$$;
INSERT INTO xreg.schema_migrations(version) VALUES (2)
ON CONFLICT (version) DO NOTHING;
COMMIT;
