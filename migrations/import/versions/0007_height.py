"""height.profile: how high the ground is along a line, as Kartverket's height service has it

A stretch has no heights of its own: they are asked of Kartverket's height
service for points along its line, and kept here so that they are asked
once. A profile belongs to a line, not to a stretch: it is found by the
checksum of the geometry, so a stretch that is moved gets a new one, a
stretch put back as it was finds its old one, and two revisions with the
same line share one.

What is kept is what was answered: the height at each point and the kind of
ground there (snow and ice, bog, lake). What a route climbs is worked out
from that when it is asked for, and so is not here.

Fetched from a source, not made by a person: it lies outside `core` and
`history`, and on this track. Taking the overlay away leaves it standing.

The schema is made by `db/bootstrap.sql`. On a database from before this
migration, run as the database's owner first:

    CREATE SCHEMA IF NOT EXISTS height AUTHORIZATION stiflyt2_owner;
    GRANT USAGE ON SCHEMA height TO stiflyt2_api;
"""
from alembic import op

revision = "0007"
down_revision = "0006"

UP = r"""
CREATE TABLE height.profile (
    line        text PRIMARY KEY,                       -- md5 of the line's geometry (EWKB)
    step_m      real NOT NULL,                          -- no further than this between two points
    sampled     geometry(LineString, 25833) NOT NULL,   -- the points asked about, in the line's own order
    at_m        real[] NOT NULL,                        -- metres along the line to each point, on the ground
    z           real[] NOT NULL,                        -- metres above sea level; NULL where the service has none
    ground      text[] NOT NULL,                        -- as the service names it: 'SnøIsbre', 'Myr', NULL
    source      text,                                   -- the service's 'datakilde', e.g. dtm1
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (cardinality(at_m) = ST_NPoints(sampled) AND cardinality(z) = cardinality(at_m)
           AND cardinality(ground) = cardinality(at_m))
);

GRANT SELECT, INSERT, UPDATE ON height.profile TO stiflyt2_api;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute("DROP TABLE height.profile")
