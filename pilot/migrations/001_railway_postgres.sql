-- Initial Railway staging schema corresponding to Service's local SQLite tables.
-- This migration is idempotent. Application cutover requires an adapter and
-- explicit tenant-scoped access controls before accepting external traffic.
BEGIN;
CREATE SCHEMA IF NOT EXISTS xreg;
CREATE TABLE IF NOT EXISTS xreg.schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS xreg.orders (
    id text PRIMARY KEY,
    data text NOT NULL
);
CREATE TABLE IF NOT EXISTS xreg.events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at double precision,
    kind text,
    order_id text,
    data text
);
CREATE TABLE IF NOT EXISTS xreg.webhooks (id text PRIMARY KEY);
CREATE TABLE IF NOT EXISTS xreg.gates (
    name text PRIMARY KEY,
    healthy integer NOT NULL CHECK (healthy IN (0, 1))
);
CREATE TABLE IF NOT EXISTS xreg.reports (
    order_id text NOT NULL,
    run integer NOT NULL,
    name text NOT NULL,
    content bytea NOT NULL,
    expires double precision NOT NULL,
    PRIMARY KEY (order_id, run, name)
);
CREATE TABLE IF NOT EXISTS xreg.maintenance (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code text UNIQUE,
    status text,
    evidence text
);
CREATE TABLE IF NOT EXISTS xreg.submissions (
    order_id text NOT NULL,
    key text NOT NULL,
    payload_hash text,
    result text,
    PRIMARY KEY (order_id, key)
);
CREATE TABLE IF NOT EXISTS xreg.checkouts (
    key_hash text PRIMARY KEY,
    payload_hash text,
    order_id text
);
INSERT INTO xreg.schema_migrations(version) VALUES (1)
ON CONFLICT (version) DO NOTHING;
COMMIT;
