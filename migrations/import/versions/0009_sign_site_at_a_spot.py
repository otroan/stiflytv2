"""A sign site at a spot on a route that is no junction

Signs stand where routes end and meet, and the graph says where that is.
They also stand elsewhere: where a path leaves a road, at a bridge, at the
edge of a car park that is no place of ours. A person puts such a site on a
route, at a spot on one of its stretches. It is found again by the stretch
and by where it lies, so it stays where it is when the stretch is divided.
"""
from alembic import op

revision = "0009"
down_revision = "0008"

UP = r"""
ALTER TABLE ops.sign_site DROP CONSTRAINT sign_site_at_type_check;
ALTER TABLE ops.sign_site ADD CONSTRAINT sign_site_at_type_check CHECK (at_type IN ('place', 'node', 'point'));

CREATE TABLE ops.sign_point (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    edge_id     uuid NOT NULL,                              -- the stretch it stands on
    geom        geometry(Point, 25833) NOT NULL,            -- where on it
    removed     boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text
);
GRANT SELECT, INSERT, UPDATE ON ops.sign_point TO stiflyt2_api;
"""

DOWN = r"""
DROP TABLE ops.sign_point;
DELETE FROM ops.sign_site WHERE at_type = 'point';
ALTER TABLE ops.sign_site DROP CONSTRAINT sign_site_at_type_check;
ALTER TABLE ops.sign_site ADD CONSTRAINT sign_site_at_type_check CHECK (at_type IN ('place', 'node'));
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
