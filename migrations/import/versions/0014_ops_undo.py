"""What is decided in operations can be taken back, and a decision that has lost its sign can be forgotten

Every change to a sign site, a post, a sign or a spot leaves the row as it was
in `ops.undo_log`, grouped by the transaction that made it, as the routes' own
changes do in `core.undo_log`. An event says which transaction it was made in,
so that an action is known by who made it and how. A row that was added is
taken away again when the action is undone, and a decision that no longer
finds what it was about can be forgotten: the service may delete here.

An order that has been taken out stands (`stands`): it has gone to whoever
makes the signs.
"""
from alembic import op

revision = "0014"
down_revision = "0013"

TABLES = ("sign_site", "sign_post", "sign", "sign_point")

UP = r"""
ALTER TABLE ops.event ADD COLUMN txid bigint;
ALTER TABLE ops.event ALTER COLUMN txid SET DEFAULT txid_current();
CREATE INDEX event_txid ON ops.event (txid);

CREATE TABLE ops.undo_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    txid       bigint NOT NULL DEFAULT txid_current(),
    at         timestamptz NOT NULL DEFAULT now(),
    tbl        text NOT NULL,
    op         text NOT NULL CHECK (op IN ('I', 'U', 'D')),
    key        uuid NOT NULL,          -- the row's id
    old        jsonb,                  -- the row as it was, for U and D
    undone_at  timestamptz,
    stands     boolean NOT NULL DEFAULT false
);
CREATE INDEX undo_log_txid ON ops.undo_log (txid);

CREATE FUNCTION ops.log_for_undo() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    r record;
    was jsonb;
BEGIN
    IF current_setting('stiflyt2.undoing', true) = 'on' THEN
        RETURN NULL;        -- putting rows back is not itself something to undo
    END IF;
    IF TG_OP = 'INSERT' THEN r := NEW; ELSE r := OLD; END IF;
    was := to_jsonb(r);
    IF was ? 'geom' THEN    -- as text the geometry comes back exactly as it was
        was := was || jsonb_build_object('geom', r.geom::text);
    END IF;
    INSERT INTO ops.undo_log (tbl, op, key, old)
    VALUES (TG_TABLE_NAME, left(TG_OP, 1), r.id, CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE was END);
    RETURN NULL;
END $$;

GRANT SELECT, INSERT, UPDATE ON ops.undo_log TO stiflyt2_api;
GRANT DELETE ON ops.sign_site, ops.sign_post, ops.sign, ops.sign_point TO stiflyt2_api;
"""


def upgrade() -> None:
    op.execute(UP)
    for table in TABLES:
        op.execute(f"""CREATE TRIGGER undo_log AFTER INSERT OR UPDATE OR DELETE ON ops.{table}
                       FOR EACH ROW EXECUTE FUNCTION ops.log_for_undo()""")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER undo_log ON ops.{table}")
    op.execute("""
        DROP FUNCTION ops.log_for_undo();
        DROP TABLE ops.undo_log;
        REVOKE DELETE ON ops.sign_site, ops.sign_post, ops.sign, ops.sign_point FROM stiflyt2_api;
        DROP INDEX ops.event_txid;
        ALTER TABLE ops.event DROP COLUMN txid""")
