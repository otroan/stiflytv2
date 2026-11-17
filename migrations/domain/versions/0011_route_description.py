"""What a route is like, in words

A route has a code, a name, who maintains it and a grading. Some routes need
more said about them than that: where they go when nothing on the ground
shows it, what they ask of the one who walks them (a guide, glacier
equipment), why they are discontinued. The description is free text on the
route, kept with it and handed on with it.
"""
from alembic import op

revision = "d0011"
down_revision = "d0010"


def upgrade() -> None:
    op.execute("ALTER TABLE core.route ADD COLUMN description text")


def downgrade() -> None:
    op.execute("ALTER TABLE core.route DROP COLUMN description")
