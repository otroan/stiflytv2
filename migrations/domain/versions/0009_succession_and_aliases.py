"""What came instead, and what a thing was called before

A route, a junction, a stretch and a place each have an id of their own that
never changes while they are moved, re-laid or renamed. Three changes do end an
id: two junctions joined, a stretch divided, a route taken into another. From
now on each of them says what came instead, in core.succession, so that
whatever points at the old id (a sign, a diary entry, a link) can be led to
what is there now.

core.alias keeps the codes and names a route or a place has had, so that a
lookup by an earlier one still finds it.

Both are filled here for what has already been done, from the event log. Rows
are added by the service with the change that causes them, and undo takes them
away with that change.
"""
from alembic import op

revision = "d0009"
down_revision = "d0008"

# The change was not taken back afterwards.
STANDS = """NOT EXISTS (SELECT 1 FROM core.undo_log u
                        WHERE u.txid = e.txid AND u.undone_at IS NOT NULL)"""


def upgrade() -> None:
    op.execute("""
        CREATE TABLE core.succession (
            id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            subject_type  text NOT NULL CHECK (subject_type IN ('route', 'node', 'edge', 'place')),
            old_id        uuid NOT NULL,
            new_id        uuid NOT NULL CHECK (new_id <> old_id),
            how           text NOT NULL CHECK (how IN ('joined', 'divided', 'absorbed')),
            at            timestamptz NOT NULL DEFAULT now(),
            UNIQUE (subject_type, old_id, new_id)
        )""")
    op.execute("""
        CREATE TABLE core.alias (
            id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            subject_type  text NOT NULL CHECK (subject_type IN ('route', 'place')),
            subject_id    uuid NOT NULL,
            kind          text NOT NULL CHECK (kind IN ('code', 'name')),
            value         text NOT NULL CHECK (value <> ''),
            until         timestamptz NOT NULL DEFAULT now()     -- when it stopped being what the thing is called
        )""")
    op.execute("CREATE UNIQUE INDEX alias_once ON core.alias (subject_type, subject_id, kind, lower(value))")
    op.execute("CREATE INDEX alias_value ON core.alias (lower(value))")

    # What has been done already. The latest of each kind per thing, and only
    # where the thing is still retired: a junction put back since is itself again.
    op.execute(f"""
        INSERT INTO core.succession (subject_type, old_id, new_id, how, at)
        SELECT DISTINCT ON (e.subject_id) 'node', e.subject_id, m.id, 'joined', e.at
        FROM history.event e
        JOIN core.node n ON n.id = e.subject_id AND n.status = 'retired'
        JOIN core.node m ON m.id::text = e.payload->>'into' AND m.id <> n.id
        WHERE e.type = 'node.merged' AND {STANDS}
        ORDER BY e.subject_id, e.seq DESC""")
    op.execute(f"""
        INSERT INTO core.succession (subject_type, old_id, new_id, how, at)
        SELECT 'edge', latest.subject_id, part.id, 'divided', latest.at
        FROM (SELECT DISTINCT ON (e.subject_id) e.subject_id, e.at, e.payload
              FROM history.event e
              JOIN core.edge x ON x.id = e.subject_id AND x.status = 'retired'
              WHERE e.type = 'edge.split' AND {STANDS}
              ORDER BY e.subject_id, e.seq DESC) latest
        CROSS JOIN LATERAL jsonb_array_elements_text(latest.payload->'into') AS into_id
        JOIN core.edge part ON part.id::text = into_id AND part.id <> latest.subject_id""")
    op.execute(f"""
        INSERT INTO core.succession (subject_type, old_id, new_id, how, at)
        SELECT DISTINCT ON (e.subject_id) 'route', e.subject_id, taker.subject_id, 'absorbed', e.at
        FROM history.event e
        JOIN core.route r ON r.id = e.subject_id AND r.status = 'retired',
             LATERAL (SELECT p.subject_id FROM history.event p
                      WHERE p.changeset_id = e.changeset_id AND p.seq > e.seq
                        AND p.type = 'route.piece_added' AND p.payload->>'was_a_route_of_yours' = 'true'
                        AND p.subject_id <> e.subject_id
                      ORDER BY p.seq LIMIT 1) taker
        WHERE e.type = 'route.joined_into' AND {STANDS}
        ORDER BY e.subject_id, e.seq DESC""")
    for subject, event, kind in (("route", "route.code_changed", "code"),
                                 ("route", "route.renamed", "name"),
                                 ("place", "place.renamed", "name")):
        op.execute(f"""
            INSERT INTO core.alias (subject_type, subject_id, kind, value, until)
            SELECT '{subject}', e.subject_id, '{kind}', e.payload->>'from', e.at
            FROM history.event e
            WHERE e.type = '{event}' AND coalesce(e.payload->>'from', '') <> '' AND {STANDS}
              AND EXISTS (SELECT 1 FROM core.{subject} t WHERE t.id = e.subject_id)
            ORDER BY e.seq
            ON CONFLICT DO NOTHING""")

    # Only now the triggers: what was filled in above is not a change to undo.
    for table in ("succession", "alias"):
        op.execute(f"GRANT SELECT, INSERT, DELETE ON core.{table} TO stiflyt2_api")
        op.execute(f"""CREATE TRIGGER undo_log AFTER INSERT OR UPDATE OR DELETE ON core.{table}
                       FOR EACH ROW EXECUTE FUNCTION core.log_for_undo()""")


def downgrade() -> None:
    op.execute("DROP TABLE core.alias")
    op.execute("DROP TABLE core.succession")
