"""Seasons: a route is a summer route or a winter route, and its stretches are of its season

DNT's winter routes (the summer code with a v, staked anew each winter) are
routes of their own. They share the places and the arrival points with the
summer routes, never a stretch: a winter line runs over frozen lakes and bogs,
and moving a summer path must not move it, nor the other way round.

`season` on a route and on a stretch says which. A leg (a stretch of a route)
carries the season of its route, set here whatever the code that adds it, and
two foreign keys refuse a leg whose stretch is of another season than its
route: no code that is wrong can share a stretch between the seasons. Junctions
have no season: a hut is where both arrive.
"""
from alembic import op

revision = "d0019"
down_revision = "d0018"

UP = r"""
ALTER TABLE core.route ADD COLUMN season text NOT NULL DEFAULT 'summer'
    CONSTRAINT route_season_known CHECK (season IN ('summer', 'winter'));
ALTER TABLE core.edge ADD COLUMN season text NOT NULL DEFAULT 'summer'
    CONSTRAINT edge_season_known CHECK (season IN ('summer', 'winter'));
ALTER TABLE core.route ADD CONSTRAINT route_id_season UNIQUE (id, season);
ALTER TABLE core.edge ADD CONSTRAINT edge_id_season UNIQUE (id, season);
CREATE INDEX route_season ON core.route (season);
CREATE INDEX edge_season ON core.edge (season);

ALTER TABLE core.route_leg ADD COLUMN season text NOT NULL DEFAULT 'summer';
CREATE FUNCTION core.leg_season() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    SELECT season INTO NEW.season FROM core.route WHERE id = NEW.route_id;
    RETURN NEW;
END $$;
CREATE TRIGGER leg_season BEFORE INSERT OR UPDATE OF route_id, season ON core.route_leg
    FOR EACH ROW EXECUTE FUNCTION core.leg_season();
ALTER TABLE core.route_leg ADD CONSTRAINT leg_route_season
    FOREIGN KEY (route_id, season) REFERENCES core.route (id, season);
ALTER TABLE core.route_leg ADD CONSTRAINT leg_edge_season
    FOREIGN KEY (edge_id, season) REFERENCES core.edge (id, season);
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE core.route_leg DROP CONSTRAINT leg_edge_season, DROP CONSTRAINT leg_route_season;
        DROP TRIGGER leg_season ON core.route_leg;
        DROP FUNCTION core.leg_season();
        ALTER TABLE core.route_leg DROP COLUMN season;
        ALTER TABLE core.edge DROP CONSTRAINT edge_id_season, DROP COLUMN season;
        ALTER TABLE core.route DROP CONSTRAINT route_id_season, DROP COLUMN season""")
