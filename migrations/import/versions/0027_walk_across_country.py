"""walk: how high each stretch lies, and a lattice over the open ground between the paths

`walk.edge.z` is the height of a stretch (the mean of its ends and middle),
`high` how far it is up in the mountains, from 0 in the valley to 1 above the
tree line: for a walk that keeps to the heights, or to the valleys.

`walk.field` is a lattice over land every 500 m, eight ways from each point,
for walking across country where there is no path: not over water or a river,
nor where the ground is steeper than a walker goes; over a glacier only when
asked for, and slower over bog. Each point is tied to the nearest junction of
the network in its square. Filled in when the network is built, from the
height model (`terrain`) and OpenStreetMap's water, rivers, bogs and glaciers.
"""
from alembic import op

revision = "0027"
down_revision = "0026"

UP = r"""
ALTER TABLE walk.edge ADD COLUMN z real, ADD COLUMN high real;

CREATE TABLE walk.field (
    id          bigint PRIMARY KEY,
    source      bigint NOT NULL,
    target      bigint NOT NULL,
    kind        text NOT NULL,              -- field (between two points of the lattice) or tie (to a junction)
    length_m    real NOT NULL,
    up_m        real NOT NULL,              -- climbed from source to target
    down_m      real NOT NULL,
    z           real,
    high        real,
    wet         boolean NOT NULL DEFAULT false,     -- mostly bog
    ice         boolean NOT NULL DEFAULT false,     -- mostly glacier
    geom        geometry(LineString, 25833) NOT NULL,
    x1 float8 NOT NULL, y1 float8 NOT NULL, x2 float8 NOT NULL, y2 float8 NOT NULL
);
CREATE INDEX field_geom ON walk.field USING gist (geom);
GRANT SELECT ON walk.field TO stiflyt2_api;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute("DROP TABLE walk.field; ALTER TABLE walk.edge DROP COLUMN z, DROP COLUMN high")
