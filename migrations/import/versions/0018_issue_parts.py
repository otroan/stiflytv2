"""An issue at a spot or along parts of a route, kept as positions along its stretches, with no line of its own

Clearing is wanted from one bend to another, not from one junction to the
next, and an issue may have several such parts. Where on a route a spot or a
part is, is kept as positions along the stretches of `core`: a stretch and
how far along it (0 at its first end, 1 at its last). Nothing of the line is
copied: when a stretch's line is mended (laid on the mapped path), the spot
and the part are drawn along the mended line; when a stretch is divided, a
position is found again on the part it lies on (ADR-004).

  along, for a spot:   [{"edge": id, "at": 0.43}]
  along, for a part:   [{"from": {"edge": id, "at": 0.4}, "to": {"edge": id, "at": 0.1}}, ...]
  along, for a route:  []

What was said of a whole stretch becomes a part from one end of it to the
other; a spot's own position becomes where it lies along its stretch.
"""
from alembic import op

revision = "0018"
down_revision = "0017"

UP = r"""
ALTER TABLE ops.entry DROP CONSTRAINT entry_about_check;
ALTER TABLE ops.entry ADD COLUMN along jsonb NOT NULL DEFAULT '[]';
-- Only where there are spots: this track can run before the one that makes `core`.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM ops.entry WHERE about = 'spot') THEN
        EXECUTE $q$
            UPDATE ops.entry n SET along = jsonb_build_array(jsonb_build_object('edge', n.edge_ids[1],
                       'at', (SELECT ST_LineLocatePoint(v.geom, n.geom) FROM core.edge e
                              JOIN core.edge_revision v ON v.id = e.current_revision_id WHERE e.id = n.edge_ids[1])))
            WHERE about = 'spot' AND geom IS NOT NULL AND cardinality(edge_ids) > 0 $q$;
    END IF;
END $$;
UPDATE ops.entry SET about = 'part', along = (SELECT jsonb_agg(jsonb_build_object(
           'from', jsonb_build_object('edge', e, 'at', 0), 'to', jsonb_build_object('edge', e, 'at', 1))) FROM unnest(edge_ids) e)
WHERE about = 'stretch';
ALTER TABLE ops.entry DROP COLUMN geom;
ALTER TABLE ops.entry ADD CONSTRAINT entry_about_check CHECK (about IN ('spot', 'part', 'route'));
ALTER TABLE ops.entry ADD CONSTRAINT entry_along_check CHECK (jsonb_typeof(along) = 'array'
    AND (about = 'route') = (jsonb_array_length(along) = 0) AND (about <> 'spot' OR jsonb_array_length(along) = 1));
"""

DOWN = r"""
ALTER TABLE ops.entry DROP CONSTRAINT entry_along_check;
ALTER TABLE ops.entry DROP CONSTRAINT entry_about_check;
ALTER TABLE ops.entry ADD COLUMN geom geometry(Point, 25833);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM ops.entry WHERE about = 'spot') THEN
        EXECUTE $q$
            UPDATE ops.entry n SET geom = (SELECT ST_LineInterpolatePoint(v.geom, (n.along -> 0 ->> 'at')::float8) FROM core.edge e
                                           JOIN core.edge_revision v ON v.id = e.current_revision_id WHERE e.id = (n.along -> 0 ->> 'edge')::uuid)
            WHERE about = 'spot' $q$;
    END IF;
END $$;
UPDATE ops.entry SET about = 'stretch' WHERE about = 'part';
ALTER TABLE ops.entry DROP COLUMN along;
ALTER TABLE ops.entry ADD CONSTRAINT entry_about_check CHECK (about IN ('spot', 'stretch', 'route'));
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
