"""core.route.reviewed now means a person has said the route is done

Until now any change by a person set it. A route that was only changed, and
never marked, goes back to not reviewed.
"""
from alembic import op

revision = "d0006"
down_revision = "d0005"


def upgrade() -> None:
    op.execute("""UPDATE core.route r SET reviewed = false
                  WHERE r.reviewed AND NOT EXISTS (
                      SELECT 1 FROM history.event e
                      WHERE e.subject_type = 'route' AND e.subject_id = r.id
                        AND e.type = 'route.reviewed')""")


def downgrade() -> None:
    pass
