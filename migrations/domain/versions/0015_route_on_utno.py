"""Which route on UT.no a route is

UT.no shows the routes to the public, each on a page of its own with a
number. Which of them is this route is not something that can be looked up
without UT.no's API: a person says it once, and it is kept with the route.
With it, what UT.no says about the route can be fetched (its description
above all), and the route can be pointed out to UT.no when it has changed.
"""
from alembic import op

revision = "d0015"
down_revision = "d0014"


def upgrade() -> None:
    op.execute("ALTER TABLE core.route ADD COLUMN utno_id bigint CHECK (utno_id > 0)")


def downgrade() -> None:
    op.execute("ALTER TABLE core.route DROP COLUMN utno_id")
