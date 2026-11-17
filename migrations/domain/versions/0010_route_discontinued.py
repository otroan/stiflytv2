"""A route that is discontinued but kept

A route can be given up in the terrain (rockfall, a bridge gone) and still be
wanted here: for its history, and so that it is known that it was there.
Withdrawing it would say it never was a route of yours. So a route can be
said to be discontinued, since a date and for a reason. It stays an active
route with its line, its stretches and its history; it is left out of the
network checks, the planner and what is handed on.
"""
from alembic import op

revision = "d0010"
down_revision = "d0009"


def upgrade() -> None:
    op.execute("""ALTER TABLE core.route
                      ADD COLUMN discontinued_since date,
                      ADD COLUMN discontinued_why text,
                      ADD CONSTRAINT route_discontinued_says_why
                          CHECK ((discontinued_since IS NULL) = (discontinued_why IS NULL)
                                 AND discontinued_why <> '')""")


def downgrade() -> None:
    op.execute("""ALTER TABLE core.route DROP CONSTRAINT route_discontinued_says_why,
                      DROP COLUMN discontinued_since, DROP COLUMN discontinued_why""")
