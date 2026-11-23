"""A route is described in each direction: the way it is walked, and back

A route is walked from its first end to its last (the order of its stretches),
and `description` describes it that way. Walked the other way the same ground
reads differently: what was a climb is a descent, what came last comes first.
UT.no keeps a description for each direction, and so does a route here:
`description_back` is the route walked from its last end to its first.
"""
from alembic import op

revision = "d0018"
down_revision = "d0017"


def upgrade() -> None:
    op.execute("ALTER TABLE core.route ADD COLUMN description_back text")


def downgrade() -> None:
    op.execute("ALTER TABLE core.route DROP COLUMN description_back")
