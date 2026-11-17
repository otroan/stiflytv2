"""Who maintains a route, and its grading, belong to the route; a stretch holds only exceptions

Until now maintainers were a list on each stretch, taken from every route
Turrutebasen has on the segment, so a stretch shared by two routes showed both
routes' maintainers. Now the route says who maintains it, and a leg says so
only where that differs along the route. The list on the stretch revision
stays as it was recorded, and is no longer read.
"""
from alembic import op

revision = "d0007"
down_revision = "d0006"


def upgrade() -> None:
    op.execute("""ALTER TABLE core.route
                      ADD COLUMN maintainers text[] NOT NULL DEFAULT '{}',
                      ADD COLUMN grading text,
                      ADD COLUMN special_type text""")
    op.execute("ALTER TABLE core.route_leg ADD COLUMN maintainers text[]")
    op.execute("GRANT UPDATE ON core.route_leg TO stiflyt2_api")


def downgrade() -> None:
    op.execute("REVOKE UPDATE ON core.route_leg FROM stiflyt2_api")
    op.execute("ALTER TABLE core.route_leg DROP COLUMN maintainers")
    op.execute("ALTER TABLE core.route DROP COLUMN maintainers, DROP COLUMN grading, "
               "DROP COLUMN special_type")
