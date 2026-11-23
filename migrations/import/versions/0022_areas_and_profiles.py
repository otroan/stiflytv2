"""An area is its routes by their codes, and routes added to it from another; a person's profile says their areas

Breheimen is the routes whose codes begin with bre, and sun27 too: a route of
another area that the same people look after. `ops.area_route` says so, by
the route's id as it was (ADR-004: read through what stands for it now).

`access.profile` is what a person has said they work with: their areas, the
first of them the one the pages open with, and whose routes are theirs
(a maintainer; none: the home maintainer). Keyed by e-mail address, or by
the name of the one editor of a server without sign-in.
"""
from alembic import op

revision = "0022"
down_revision = "0021"

UP = r"""
CREATE TABLE ops.area_route (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    area        text NOT NULL CHECK (area ~ '^[a-z]{3}$'),
    route_id    uuid NOT NULL,
    added_at    timestamptz NOT NULL DEFAULT now(),
    added_by    text NOT NULL,
    UNIQUE (area, route_id)
);
CREATE TRIGGER undo_log AFTER INSERT OR UPDATE OR DELETE ON ops.area_route
    FOR EACH ROW EXECUTE FUNCTION ops.log_for_undo();
GRANT SELECT, INSERT, DELETE ON ops.area_route TO stiflyt2_api;

CREATE TABLE access.profile (
    who         text PRIMARY KEY,
    areas       text[] NOT NULL DEFAULT '{}',
    maintainer  text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON access.profile TO stiflyt2_api;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute("DROP TABLE ops.area_route; DROP TABLE access.profile")
