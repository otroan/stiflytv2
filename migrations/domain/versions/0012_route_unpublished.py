"""A route that is kept and not published

A route can be wanted here without being for the public: kept for its
history, or not yet ready to be shown. Discontinued is something else: that
says the route is given up in the terrain, and asks those who show it to
take it out, because it is no longer there. A route that is not published
is not handed on: it is left out of what goes to UT.no, nothing new is asked
of Kartverket for it, and where Turrutebasen has it, Kartverket is asked to
take its number off. Everything else about it is as for any route of yours.

Said with a reason. No reason means the route is published, as every route
is until something else is said.
"""
from alembic import op

revision = "d0012"
down_revision = "d0011"


def upgrade() -> None:
    op.execute("""ALTER TABLE core.route
                      ADD COLUMN unpublished_why text,
                      ADD CONSTRAINT route_unpublished_says_why CHECK (unpublished_why <> '')""")


def downgrade() -> None:
    op.execute("""ALTER TABLE core.route DROP CONSTRAINT route_unpublished_says_why,
                      DROP COLUMN unpublished_why""")
