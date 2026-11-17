"""source_raw and import_control: immutable snapshots and feature versions"""
from alembic import op

revision = "0001"
down_revision = None

UP = r"""
CREATE TABLE source_raw.source (
    key            text PRIMARY KEY,
    name           text NOT NULL,
    licence        text NOT NULL,
    attribution    text NOT NULL,
    export_policy  text NOT NULL CHECK (export_policy IN ('allowed', 'blocked', 'unresolved')),
    homepage       text
);

CREATE TABLE source_raw.snapshot (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_key         text NOT NULL REFERENCES source_raw.source(key),
    scope              text NOT NULL,
    state              text NOT NULL DEFAULT 'loading'
                       CHECK (state IN ('loading', 'complete', 'failed')),
    complete           boolean NOT NULL DEFAULT false,
    fetched_at         timestamptz NOT NULL DEFAULT now(),
    source_updated_at  timestamptz,
    checksum           text NOT NULL,
    crs                integer NOT NULL,
    artifact_path      text NOT NULL,
    transform_version  text NOT NULL,
    manifest           jsonb NOT NULL DEFAULT '{}',
    counts             jsonb NOT NULL DEFAULT '{}',
    error              text
);
-- The same file for the same scope is one snapshot, however often it is fetched.
CREATE UNIQUE INDEX snapshot_idempotent
    ON source_raw.snapshot (source_key, scope, checksum, transform_version)
    WHERE state <> 'failed';

CREATE TABLE source_raw.feature_version (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_key    text NOT NULL REFERENCES source_raw.source(key),
    layer         text NOT NULL,
    external_id   text NOT NULL,
    content_hash  bytea NOT NULL,
    properties    jsonb NOT NULL,
    geom          geometry(Geometry, 25833),
    first_seen    uuid NOT NULL REFERENCES source_raw.snapshot(id),
    UNIQUE (source_key, layer, external_id, content_hash)
);

CREATE TABLE source_raw.snapshot_member (
    snapshot_id         uuid NOT NULL REFERENCES source_raw.snapshot(id),
    feature_version_id  bigint NOT NULL REFERENCES source_raw.feature_version(id),
    PRIMARY KEY (snapshot_id, feature_version_id)
);
CREATE INDEX snapshot_member_fv ON source_raw.snapshot_member (feature_version_id);

-- Immutability is enforced in the database, not by convention.
CREATE FUNCTION source_raw.reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on %.% is not allowed: source_raw is append-only',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END $$;

CREATE TRIGGER feature_version_immutable
    BEFORE UPDATE OR DELETE ON source_raw.feature_version
    FOR EACH ROW EXECUTE FUNCTION source_raw.reject_change();
CREATE TRIGGER feature_version_no_truncate
    BEFORE TRUNCATE ON source_raw.feature_version
    FOR EACH STATEMENT EXECUTE FUNCTION source_raw.reject_change();

-- Members can be removed only while their snapshot is still loading or failed.
CREATE FUNCTION source_raw.member_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s text;
BEGIN
    SELECT state INTO s FROM source_raw.snapshot
     WHERE id = COALESCE(NEW.snapshot_id, OLD.snapshot_id);
    IF TG_OP = 'INSERT' AND s = 'loading' THEN RETURN NEW; END IF;
    IF TG_OP = 'DELETE' AND s IN ('loading', 'failed') THEN RETURN OLD; END IF;
    RAISE EXCEPTION '% on snapshot_member not allowed while snapshot is %', TG_OP, s
        USING ERRCODE = 'restrict_violation';
END $$;
CREATE TRIGGER snapshot_member_guard
    BEFORE INSERT OR UPDATE OR DELETE ON source_raw.snapshot_member
    FOR EACH ROW EXECUTE FUNCTION source_raw.member_guard();

-- A snapshot is frozen once complete; a loading one may only finish or fail.
CREATE FUNCTION source_raw.snapshot_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'snapshots are never deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.state = 'complete' THEN
        RAISE EXCEPTION 'snapshot % is complete and immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.id, NEW.source_key, NEW.scope, NEW.checksum, NEW.crs, NEW.artifact_path,
        NEW.transform_version, NEW.fetched_at)
       IS DISTINCT FROM
       (OLD.id, OLD.source_key, OLD.scope, OLD.checksum, OLD.crs, OLD.artifact_path,
        OLD.transform_version, OLD.fetched_at) THEN
        RAISE EXCEPTION 'snapshot identity columns cannot change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER snapshot_guard
    BEFORE UPDATE OR DELETE ON source_raw.snapshot
    FOR EACH ROW EXECUTE FUNCTION source_raw.snapshot_guard();

CREATE TABLE import_control.active_snapshot (
    source_key    text NOT NULL,
    scope         text NOT NULL,
    snapshot_id   uuid NOT NULL,
    activated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_key, scope)
);

CREATE TABLE import_control.activation_log (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_key    text NOT NULL,
    scope         text NOT NULL,
    snapshot_id   uuid NOT NULL,
    previous_id   uuid,
    activated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE import_control.diff (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_key     text NOT NULL,
    scope          text NOT NULL,
    from_snapshot  uuid,
    to_snapshot    uuid NOT NULL,
    counts         jsonb NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (from_snapshot, to_snapshot)
);

CREATE TABLE import_control.diff_item (
    diff_id      uuid NOT NULL REFERENCES import_control.diff(id) ON DELETE CASCADE,
    layer        text NOT NULL,
    external_id  text NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('created', 'updated', 'missing')),
    old_version  bigint,
    new_version  bigint,
    PRIMARY KEY (diff_id, layer, external_id)
);

GRANT SELECT, INSERT ON source_raw.source, source_raw.feature_version TO stiflyt2_import;
GRANT SELECT, INSERT, DELETE ON source_raw.snapshot_member TO stiflyt2_import;
GRANT SELECT, INSERT ON source_raw.snapshot TO stiflyt2_import;
GRANT UPDATE (state, complete, source_updated_at, manifest, counts, error)
    ON source_raw.snapshot TO stiflyt2_import;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA import_control TO stiflyt2_import;
GRANT SELECT ON ALL TABLES IN SCHEMA import_control TO stiflyt2_api;
"""

DOWN = r"""
DROP TABLE import_control.diff_item, import_control.diff,
           import_control.activation_log, import_control.active_snapshot;
DROP TABLE source_raw.snapshot_member, source_raw.feature_version,
           source_raw.snapshot, source_raw.source;
DROP FUNCTION source_raw.reject_change(), source_raw.member_guard(), source_raw.snapshot_guard();
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
