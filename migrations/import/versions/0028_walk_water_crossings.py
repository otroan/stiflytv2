"""walk: where a way crosses a river: by a bridge, by a ford, or wading across country

`walk.edge.ford` is a stretch that fords a river (OpenStreetMap's ford=yes on
the way or on a node of it, where it crosses a river: a stream is not counted), `bridge` one that is a bridge over a river, and `river`
the name of the river it crosses, where it has one. `walk.field.wade` is a
stretch across country that wades a river (rivers drawn as a line; those drawn
as an area are too wide, and are never waded), with the river's name.
`walk.river` holds the rivers the names come from. Filled in when the network
is built.
"""
from alembic import op

revision = "0028"
down_revision = "0027"

UP = r"""
ALTER TABLE walk.edge ADD COLUMN ford boolean NOT NULL DEFAULT false, ADD COLUMN bridge boolean NOT NULL DEFAULT false,
                      ADD COLUMN river text;
ALTER TABLE walk.field ADD COLUMN wade boolean NOT NULL DEFAULT false, ADD COLUMN river text;
CREATE TABLE walk.river (
    id      bigint PRIMARY KEY,
    name    text,
    geom    geometry(MultiLineString, 25833) NOT NULL
);
CREATE INDEX river_geom ON walk.river USING gist (geom);
GRANT SELECT ON walk.river TO stiflyt2_api;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute("DROP TABLE walk.river; ALTER TABLE walk.field DROP COLUMN wade, DROP COLUMN river; "
               "ALTER TABLE walk.edge DROP COLUMN ford, DROP COLUMN bridge, DROP COLUMN river")
