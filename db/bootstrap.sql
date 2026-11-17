-- Run once per database, connected to it:
--   createdb stiflyt2 && psql -d stiflyt2 -f db/bootstrap.sql
-- Idempotent. Creates roles, schemas and extensions. Tables come from Alembic.
--
-- Run it as a superuser, or as the database owner with CREATEROLE. Only a
-- superuser can create the PostGIS extension, so in the second case create the
-- database from a template that already has it:
--   createdb -T stiflyt2_template stiflyt2

-- A non-superuser who creates the roles must also be able to act as them, to
-- create the schemas they own. Has no effect for a superuser.
SET createrole_self_grant = 'set, inherit';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stiflyt2_owner') THEN
        CREATE ROLE stiflyt2_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stiflyt2_migrate') THEN
        CREATE ROLE stiflyt2_migrate LOGIN IN ROLE stiflyt2_owner;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stiflyt2_import') THEN
        CREATE ROLE stiflyt2_import LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stiflyt2_api') THEN
        CREATE ROLE stiflyt2_api LOGIN;
    END IF;
END $$;

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO stiflyt2_migrate, stiflyt2_import, stiflyt2_api',
        current_database());
    -- The import role loads files into temporary tables.
    EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO stiflyt2_import', current_database());
END $$;

CREATE SCHEMA IF NOT EXISTS source_raw     AUTHORIZATION stiflyt2_owner;
CREATE SCHEMA IF NOT EXISTS source_index   AUTHORIZATION stiflyt2_owner;
CREATE SCHEMA IF NOT EXISTS import_control AUTHORIZATION stiflyt2_owner;
CREATE SCHEMA IF NOT EXISTS import_staging AUTHORIZATION stiflyt2_import;
-- source_index tables are rebuilt here and then moved into place.
CREATE SCHEMA IF NOT EXISTS source_index_build AUTHORIZATION stiflyt2_import;
CREATE SCHEMA IF NOT EXISTS core           AUTHORIZATION stiflyt2_owner;
CREATE SCHEMA IF NOT EXISTS history        AUTHORIZATION stiflyt2_owner;
-- What people meet when they use the tool. About the tool, not about the routes:
-- it is not part of the overlay and stays when the overlay is taken away.
CREATE SCHEMA IF NOT EXISTS feedback       AUTHORIZATION stiflyt2_owner;
-- Who may use the tool, and as what. The API reads it and cannot change who is in it.
CREATE SCHEMA IF NOT EXISTS access         AUTHORIZATION stiflyt2_owner;

-- How high the ground is along the lines, as fetched from Kartverket's height service. Fetched,
-- not made by a person: it stays when the overlay is taken away.
CREATE SCHEMA IF NOT EXISTS height         AUTHORIZATION stiflyt2_owner;

-- What happens to the routes: signs, and later work, reports, closures. It points at the routes
-- and never changes them.
CREATE SCHEMA IF NOT EXISTS ops            AUTHORIZATION stiflyt2_owner;

-- Import side: usage only. Table-level grants are set by the migrations.
GRANT USAGE ON SCHEMA source_raw, source_index, import_control TO stiflyt2_import;
GRANT CREATE ON SCHEMA source_index TO stiflyt2_import;

-- API side: reads the index and job state, owns nothing on the import side.
GRANT USAGE ON SCHEMA source_index, import_control, core, history, feedback, access, height, ops TO stiflyt2_api;

-- The hard wall: the import role has no access to core or history at all.
REVOKE ALL ON SCHEMA core, history FROM stiflyt2_import;
REVOKE ALL ON SCHEMA core, history FROM PUBLIC;
REVOKE ALL ON SCHEMA source_raw FROM stiflyt2_api;
