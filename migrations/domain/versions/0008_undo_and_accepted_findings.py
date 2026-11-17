"""Undo for every change, and findings a person has said are right as they are

Undo: each change to a route, a leg, a node, an edge or a place writes the row
as it was to core.undo_log, grouped by the transaction that made the change
(one request is one action). Undoing the latest action puts those rows back,
newest first. Rows the action added cannot be deleted by the service, and edge
revisions and events are never deleted: an added route, node, edge or place is
given the status `undone` instead, and the later revisions simply stay.

Findings: core.accepted_finding holds the key of a network finding that a
person has looked at and found right (an isolated route, a dead end at a
summit), so that it is not listed again.
"""
from alembic import op

revision = "d0008"
down_revision = "d0007"

TABLES = ("route", "route_leg", "node", "edge", "place")


def upgrade() -> None:
    op.execute("ALTER TABLE history.event ADD COLUMN txid bigint")
    op.execute("ALTER TABLE history.event ALTER COLUMN txid SET DEFAULT txid_current()")
    for table, first in (("route", "active"), ("node", "active"), ("edge", "active"),
                         ("place", "confirmed")):
        op.execute(f"ALTER TABLE core.{table} DROP CONSTRAINT {table}_status_check")
        op.execute(f"""ALTER TABLE core.{table} ADD CONSTRAINT {table}_status_check
                       CHECK (status IN ('{first}', 'retired', 'undone'))""")
    op.execute("""
        CREATE TABLE core.undo_log (
            id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            txid       bigint NOT NULL DEFAULT txid_current(),
            at         timestamptz NOT NULL DEFAULT now(),
            author     text,
            tbl        text NOT NULL,
            op         text NOT NULL CHECK (op IN ('I', 'U', 'D')),
            key        jsonb NOT NULL,     -- the row's primary key
            old        jsonb,              -- the row as it was, for U and D
            undone_at  timestamptz
        )""")
    op.execute("CREATE INDEX undo_log_txid ON core.undo_log (txid)")
    op.execute("GRANT SELECT, INSERT, UPDATE ON core.undo_log TO stiflyt2_api")
    op.execute("""
        CREATE FUNCTION core.log_for_undo() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            r record;
            was jsonb;
            k jsonb;
        BEGIN
            IF current_setting('stiflyt2.undoing', true) = 'on' THEN
                RETURN NULL;        -- putting rows back is not itself something to undo
            END IF;
            IF TG_OP = 'INSERT' THEN r := NEW; ELSE r := OLD; END IF;
            was := to_jsonb(r);
            IF was ? 'geom' THEN    -- as text the geometry comes back exactly as it was
                was := was || jsonb_build_object('geom', r.geom::text);
            END IF;
            IF TG_TABLE_NAME = 'route_leg' THEN
                k := jsonb_build_object('route_id', r.route_id, 'seq', r.seq);
            ELSE
                k := jsonb_build_object('id', r.id);
            END IF;
            INSERT INTO core.undo_log (author, tbl, op, key, old)
            VALUES (nullif(current_setting('stiflyt2.author', true), ''), TG_TABLE_NAME,
                    left(TG_OP, 1), k, CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE was END);
            RETURN NULL;
        END $$""")
    for table in TABLES:
        op.execute(f"""CREATE TRIGGER undo_log AFTER INSERT OR UPDATE OR DELETE ON core.{table}
                       FOR EACH ROW EXECUTE FUNCTION core.log_for_undo()""")
    op.execute("""
        CREATE TABLE core.accepted_finding (
            key     text PRIMARY KEY,
            kind    text NOT NULL,
            label   text NOT NULL,
            text    text NOT NULL,
            author  text NOT NULL,
            at      timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("GRANT SELECT, INSERT, DELETE ON core.accepted_finding TO stiflyt2_api")


def downgrade() -> None:
    op.execute("DROP TABLE core.accepted_finding")
    for table in TABLES:
        op.execute(f"DROP TRIGGER undo_log ON core.{table}")
    op.execute("DROP FUNCTION core.log_for_undo()")
    op.execute("DROP TABLE core.undo_log")
    for table, first in (("route", "active"), ("node", "active"), ("edge", "active"),
                         ("place", "confirmed")):
        op.execute(f"UPDATE core.{table} SET status = 'retired' WHERE status = 'undone'")
        op.execute(f"ALTER TABLE core.{table} DROP CONSTRAINT {table}_status_check")
        op.execute(f"""ALTER TABLE core.{table} ADD CONSTRAINT {table}_status_check
                       CHECK (status IN ('{first}', 'retired'))""")
    op.execute("ALTER TABLE history.event DROP COLUMN txid")
