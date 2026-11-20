"""What happens on a route: entries, first issues and notices (the feature `issues`, ADR-004, ADR-005)

An entry is something said about a route that is not what the route is: a
tree across the path, marking that has worn away, a bridge that is out, the
route closed for works. It says what it is about as ADR-004 says: by the ids
of the route and stretches as they were when it was said, read through what
stands for them now. A spot has a position of its own, which does not move
when the line of the route is mended: a fallen tree lies where it lies.

Later kinds (a report from a work party, an inspection, a diary entry) are
entries too, in the same table.
"""
from alembic import op

revision = "0017"
down_revision = "0016"

UP = r"""
CREATE TABLE ops.entry (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind         text NOT NULL CHECK (kind IN ('closed', 'warning', 'clearing', 'marking', 'sign', 'bridge', 'wet', 'other')),
    about        text NOT NULL CHECK (about IN ('spot', 'stretch', 'route')),
    route_id     uuid NOT NULL,                     -- the route it was said on, as it was
    edge_ids     uuid[] NOT NULL DEFAULT '{}',      -- a spot: the stretch it lies on; a stretch: the stretch
    geom         geometry(Point, 25833),            -- a spot: where it is; else where it was said
    area         text NOT NULL,                     -- the area of the route it was said on
    text         text NOT NULL,
    state        text NOT NULL DEFAULT 'reported' CHECK (state IN ('reported', 'planned', 'done', 'dismissed')),
    valid_from   date,                              -- a notice: from when, and until when (none: until further notice)
    valid_until  date,
    made_at      timestamptz NOT NULL DEFAULT now(),
    made_by      text NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text NOT NULL,
    CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from)
);
CREATE INDEX entry_area ON ops.entry (area);
CREATE INDEX entry_edges ON ops.entry USING gin (edge_ids);

CREATE TRIGGER undo_log AFTER INSERT OR UPDATE OR DELETE ON ops.entry
    FOR EACH ROW EXECUTE FUNCTION ops.log_for_undo();

GRANT SELECT, INSERT, UPDATE, DELETE ON ops.entry TO stiflyt2_api;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute("DROP TABLE ops.entry")
