"""A change says how it came in: from the map, from an assistant, from a script

With more than one person, and with an assistant acting as a person while
that person works in the map, "the latest change" is no longer what Undo
should take back. Undo takes back the latest change of the one who asks,
made the way they are asking: Undo in the map takes back what was done in the
map, and an assistant takes back what the assistant did. So the rows a change
leaves, and the event that tells of it, say how the change came in.

Nothing said (changes from before this, and from the command line) counts as
the map.
"""
from alembic import op

revision = "d0013"
down_revision = "d0012"

FUNCTION = """
    CREATE OR REPLACE FUNCTION core.log_for_undo() RETURNS trigger LANGUAGE plpgsql AS $$
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
        INSERT INTO core.undo_log (author, %s tbl, op, key, old)
        VALUES (nullif(current_setting('stiflyt2.author', true), ''), %s TG_TABLE_NAME,
                left(TG_OP, 1), k, CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE was END);
        RETURN NULL;
    END $$"""


def upgrade() -> None:
    op.execute("ALTER TABLE core.undo_log ADD COLUMN through text")
    op.execute("ALTER TABLE history.event ADD COLUMN through text")
    op.execute("ALTER TABLE history.event ALTER COLUMN through "
               "SET DEFAULT nullif(current_setting('stiflyt2.through', true), '')")
    op.execute(FUNCTION % ("through,", "nullif(current_setting('stiflyt2.through', true), ''),"))


def downgrade() -> None:
    op.execute(FUNCTION % ("", ""))
    op.execute("ALTER TABLE history.event DROP COLUMN through")
    op.execute("ALTER TABLE core.undo_log DROP COLUMN through")
