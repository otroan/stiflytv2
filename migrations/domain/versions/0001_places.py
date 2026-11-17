"""core and history: changesets, the event log, places, nodes, edges (ADR-002)"""
from alembic import op

revision = "d0001"
down_revision = None

UP = r"""
-- Every change to owned data happens in a changeset and is recorded as an event.
CREATE TABLE core.changeset (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title       text NOT NULL,
    comment     text,
    author      text NOT NULL,
    state       text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    closed_at   timestamptz
);

CREATE TABLE history.event (
    seq           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    changeset_id  uuid NOT NULL,
    at            timestamptz NOT NULL DEFAULT now(),
    author        text NOT NULL,
    type          text NOT NULL,
    subject_type  text NOT NULL,
    subject_id    uuid NOT NULL,
    payload       jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX event_subject ON history.event (subject_type, subject_id, seq);
CREATE INDEX event_changeset ON history.event (changeset_id, seq);

CREATE FUNCTION history.reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on %.% is not allowed: history is append-only',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END $$;
CREATE TRIGGER event_immutable BEFORE UPDATE OR DELETE ON history.event
    FOR EACH ROW EXECUTE FUNCTION history.reject_change();
CREATE TRIGGER event_no_truncate BEFORE TRUNCATE ON history.event
    FOR EACH STATEMENT EXECUTE FUNCTION history.reject_change();

-- A place is a logical destination: what a route runs to, what a sign names.
CREATE TABLE core.place (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name             text NOT NULL,
    kind             text NOT NULL CHECK (kind IN ('hut', 'parking', 'other')),
    geom             geometry(Point, 25833) NOT NULL,   -- reference point, e.g. the building
    primary_node_id  uuid,
    status           text NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed', 'retired')),
    version          integer NOT NULL DEFAULT 1,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX place_geom ON core.place USING gist (geom);

-- A node is a point in the graph, where it is on the ground. A node with a
-- place_id is an access point of that place.
CREATE TABLE core.node (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    geom        geometry(Point, 25833) NOT NULL,
    kind        text NOT NULL DEFAULT 'endpoint' CHECK (kind IN ('junction', 'endpoint')),
    name        text,
    place_id    uuid REFERENCES core.place(id),
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    version     integer NOT NULL DEFAULT 1,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX node_geom ON core.node USING gist (geom);
CREATE INDEX node_place ON core.node (place_id);
ALTER TABLE core.place ADD CONSTRAINT place_primary_node
    FOREIGN KEY (primary_node_id) REFERENCES core.node(id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE core.edge (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    status               text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    current_revision_id  uuid,
    version              integer NOT NULL DEFAULT 1,
    created_at           timestamptz NOT NULL DEFAULT now()
);

-- The geometry of an edge at one point in time. Never changed: an edit is a new revision.
CREATE TABLE core.edge_revision (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    edge_id       uuid NOT NULL REFERENCES core.edge(id),
    rev_no        integer NOT NULL,
    geom          geometry(LineString, 25833) NOT NULL,
    a_node        uuid NOT NULL REFERENCES core.node(id),
    b_node        uuid NOT NULL REFERENCES core.node(id),
    kind          text NOT NULL CHECK (kind IN ('trail', 'tractor_road', 'road', 'glacier', 'water',
                                                'off_trail', 'connector', 'other')),
    origin        text NOT NULL CHECK (origin IN ('adopted', 'edited', 'drawn', 'derived')),
    changeset_id  uuid NOT NULL REFERENCES core.changeset(id),
    author        text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (edge_id, rev_no)
);
CREATE INDEX edge_revision_geom ON core.edge_revision USING gist (geom);
CREATE FUNCTION core.reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on %.% is not allowed: revisions are immutable',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END $$;
CREATE TRIGGER edge_revision_immutable BEFORE UPDATE OR DELETE ON core.edge_revision
    FOR EACH ROW EXECUTE FUNCTION core.reject_change();
ALTER TABLE core.edge ADD CONSTRAINT edge_current_revision
    FOREIGN KEY (current_revision_id) REFERENCES core.edge_revision(id) DEFERRABLE INITIALLY DEFERRED;

-- What an owned object was made from, as plain values. Nothing here is a key
-- into the import side, so the sources can change or vanish freely.
CREATE TABLE core.source_reference (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_type  text NOT NULL CHECK (subject_type IN ('place', 'node', 'edge')),
    subject_id    uuid NOT NULL,
    source_key    text NOT NULL,
    external_id   text NOT NULL,
    snapshot_id   uuid,
    ref_type      text NOT NULL CHECK (ref_type IN ('adopted_from', 'traced', 'evidence', 'claim')),
    detail        jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX source_reference_subject ON core.source_reference (subject_type, subject_id);

GRANT SELECT, INSERT, UPDATE ON core.changeset, core.place, core.node, core.edge TO stiflyt2_api;
GRANT SELECT, INSERT ON core.edge_revision, core.source_reference TO stiflyt2_api;
GRANT SELECT, INSERT ON history.event TO stiflyt2_api;
"""

DOWN = r"""
DROP TABLE core.source_reference;
ALTER TABLE core.edge DROP CONSTRAINT edge_current_revision;
ALTER TABLE core.place DROP CONSTRAINT place_primary_node;
DROP TABLE core.edge_revision, core.edge, core.node, core.place, history.event, core.changeset;
DROP FUNCTION core.reject_change(), history.reject_change();
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
