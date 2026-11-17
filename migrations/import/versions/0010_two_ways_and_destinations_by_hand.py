"""Two signs to one place by two ways, and a destination a person adds to a post

Two routes can leave a junction along the same stretch and reach the same
place by different ways, with different distances (bre8 over the glacier and
bre9 round it, from Sota sæter to Nørdstedalseter). Those are two signs. They
are told apart by a route of each (`route`), which is empty for a sign that
is the only one along its stretch to its place.

A person can add a destination to a post that the routes do not give: a
place nearer than the next hut, or further on than it (`added`). Such a sign
points to a place of ours, or to a spot on a route that the sign names
(`to_type = 'point'`, a row in `ops.sign_point`). Which way it points is the
shortest way there over the routes, worked out each time, so it has no
stretch of its own: `first_edge` is the nil uuid.
"""
from alembic import op

revision = "0010"
down_revision = "0009"

UP = r"""
ALTER TABLE ops.sign ADD COLUMN route text NOT NULL DEFAULT '';
ALTER TABLE ops.sign ADD COLUMN added boolean NOT NULL DEFAULT false;
ALTER TABLE ops.sign DROP CONSTRAINT sign_node_id_first_edge_to_type_to_id_key;
ALTER TABLE ops.sign ADD CONSTRAINT sign_found_by UNIQUE (node_id, first_edge, to_type, to_id, route);
ALTER TABLE ops.sign DROP CONSTRAINT sign_to_type_check;
ALTER TABLE ops.sign ADD CONSTRAINT sign_to_type_check CHECK (to_type IN ('place', 'node', 'point'));
"""

DOWN = r"""
DELETE FROM ops.sign WHERE added OR route <> '' OR to_type = 'point';
ALTER TABLE ops.sign DROP CONSTRAINT sign_to_type_check;
ALTER TABLE ops.sign ADD CONSTRAINT sign_to_type_check CHECK (to_type IN ('place', 'node'));
ALTER TABLE ops.sign DROP CONSTRAINT sign_found_by;
ALTER TABLE ops.sign ADD CONSTRAINT sign_node_id_first_edge_to_type_to_id_key UNIQUE (node_id, first_edge, to_type, to_id);
ALTER TABLE ops.sign DROP COLUMN added, DROP COLUMN route;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
