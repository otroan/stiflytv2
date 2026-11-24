"""walk: the network the route planner walks on (the feature `planner`)

Every way in Norway one may walk on, from OpenStreetMap's extract of Norway,
cut where ways meet, with Turrutebasen's marked routes and our own where
OpenStreetMap has nothing along them. Made from sources, rebuilt when a new
extract has been fetched (`stiflyt2 planner build`): nothing here is decided
by a person, and it stays when the overlay is taken away.

The tables here are empty until the network is first built. A build makes
new ones beside them and puts them in their place in one go.

The schema is made by `db/bootstrap.sql`. On a database from before this
migration, run as the database's owner first:

    CREATE SCHEMA IF NOT EXISTS walk AUTHORIZATION stiflyt2_owner;
    GRANT USAGE ON SCHEMA walk TO stiflyt2_api;

and as a superuser, unless the database was made from a template that has it:

    CREATE EXTENSION IF NOT EXISTS pgrouting;
"""
from alembic import op

revision = "0025"
down_revision = "0024"

UP = r"""
-- A stretch between two junctions, as pgRouting wants it: from vertex `source` to vertex `target`, and
-- both ways (walking has no one-way streets). A vertex is an OpenStreetMap node where it is one, else a
-- number made from where it lies (`walknet.VERTEX`).
CREATE TABLE walk.edge (
    id          bigint PRIMARY KEY,
    source      bigint NOT NULL,
    target      bigint NOT NULL,
    origin      text NOT NULL,          -- osm, turrutebasen, ours, link (a step joining a line to the network)
    osm_id      bigint,                 -- the way it is part of, for what is from OpenStreetMap
    kind        text NOT NULL,          -- OpenStreetMap's highway=, or ferry, or trail for a line of ours or Turrutebasen's
    name        text,
    sac         text,                   -- sac_scale: how demanding the path is
    visibility  text,                   -- trail_visibility
    private     boolean NOT NULL DEFAULT false,     -- access private or no, and not said open on foot
    tunnel      boolean NOT NULL DEFAULT false,
    discouraged boolean NOT NULL DEFAULT false,     -- foot=discouraged or use_sidepath
    trail       boolean NOT NULL DEFAULT false,     -- along a marked route in Turrutebasen, or one of ours
    dnt         boolean NOT NULL DEFAULT false,     -- along a route DNT looks after
    ice         boolean NOT NULL DEFAULT false,     -- mostly on a glacier
    length_m    real NOT NULL,
    geom        geometry(LineString, 25833) NOT NULL,
    x1 float8 NOT NULL, y1 float8 NOT NULL, x2 float8 NOT NULL, y2 float8 NOT NULL
);
CREATE INDEX edge_geom ON walk.edge USING gist (geom);

-- Huts, from OpenStreetMap (tourism=alpine_hut or wilderness_hut): where a day may end.
CREATE TABLE walk.hut (
    osm_id      text PRIMARY KEY,       -- n123 or w123 or r123
    name        text,
    kind        text NOT NULL,          -- alpine_hut (staffed or self-service) or wilderness_hut
    operator    text,
    dnt         boolean NOT NULL DEFAULT false,
    geom        geometry(Point, 25833) NOT NULL
);
CREATE INDEX hut_geom ON walk.hut USING gist (geom);

-- What the network was built from, and when. One row: the latest build.
CREATE TABLE walk.built (
    one         boolean PRIMARY KEY DEFAULT true CHECK (one),
    extract_md5 text NOT NULL,          -- Geofabrik's checksum of the extract
    extract_at  timestamptz,            -- when Geofabrik made it
    built_at    timestamptz NOT NULL DEFAULT now(),
    took_s      real,
    counts      jsonb NOT NULL DEFAULT '{}'
);

GRANT SELECT ON walk.edge, walk.hut, walk.built TO stiflyt2_api;
ALTER DEFAULT PRIVILEGES FOR ROLE stiflyt2_owner IN SCHEMA walk GRANT SELECT ON TABLES TO stiflyt2_api;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute("DROP TABLE walk.edge, walk.hut, walk.built")
