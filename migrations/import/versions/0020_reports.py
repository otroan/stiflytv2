"""Reports of what was done on the routes: inspections, work parties (dugnad), diary notes (the feature `reports`)

A report says what was done when, and where: whole routes, or parts of them,
kept as positions along stretches as issues are (ADR-004). For a whole route
the stretches it walked that day are kept, so that a route carried on later
is not said to have been inspected where it was not. When a route was last
inspected, or last had a work party, is worked out from the reports, per
stretch: nobody keeps it by hand.

A report may say issues are done (`ops.entry.closed_by`), and photos may be
of it (`ops.photo.report_id`). The documents themselves are elsewhere (a link).
"""
from alembic import op

revision = "0020"
down_revision = "0019"

UP = r"""
CREATE TABLE ops.report (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        text NOT NULL CHECK (kind IN ('inspection', 'work', 'note')),
    done_on     date NOT NULL,
    area        text NOT NULL,
    text        text,
    condition   text CHECK (condition IN ('good', 'fair', 'poor')),        -- an inspection: the state found
    persons     integer CHECK (persons >= 0 AND persons < 1000),           -- a work party: how many
    hours       numeric(7, 1) CHECK (hours >= 0 AND hours < 100000),        -- and the hours of all of them
    done        text[] NOT NULL DEFAULT '{}',                               -- what was done: clearing, marking, …
    link        text,                                                       -- where the report itself is kept
    made_at     timestamptz NOT NULL DEFAULT now(),
    made_by     text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL
);
CREATE INDEX report_area ON ops.report (area, done_on);

-- What a report covers: a whole route (the stretches it walked that day), or parts of it.
CREATE TABLE ops.report_part (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id   uuid NOT NULL,
    no          smallint NOT NULL DEFAULT 0,        -- in the order the report says them
    route_id    uuid NOT NULL,
    along       jsonb NOT NULL DEFAULT '[]',        -- []: the whole route; else [{"from": {edge, at}, "to": {edge, at}}, …]
    edge_ids    uuid[] NOT NULL DEFAULT '{}'        -- the whole route: its stretches then; parts: where they begin and end
);
CREATE INDEX report_part_report ON ops.report_part (report_id);
CREATE INDEX report_part_route ON ops.report_part (route_id);
CREATE INDEX report_part_edges ON ops.report_part USING gin (edge_ids);

ALTER TABLE ops.entry ADD COLUMN closed_by uuid;    -- the report that said it is done
ALTER TABLE ops.photo ADD COLUMN report_id uuid;    -- the report it is of
CREATE INDEX photo_report ON ops.photo (report_id);

CREATE TRIGGER undo_log AFTER INSERT OR UPDATE OR DELETE ON ops.report
    FOR EACH ROW EXECUTE FUNCTION ops.log_for_undo();
CREATE TRIGGER undo_log AFTER INSERT OR UPDATE OR DELETE ON ops.report_part
    FOR EACH ROW EXECUTE FUNCTION ops.log_for_undo();

GRANT SELECT, INSERT, UPDATE, DELETE ON ops.report, ops.report_part TO stiflyt2_api;
"""

DOWN = r"""
DROP TABLE ops.report_part, ops.report;
ALTER TABLE ops.entry DROP COLUMN closed_by;
ALTER TABLE ops.photo DROP COLUMN report_id;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
