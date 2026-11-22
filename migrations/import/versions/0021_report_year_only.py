"""A report of which only the year is known: as from an area contact's overview, kept by the year

It is told as the year. How long since is counted from the middle of it, the 1st of July.
"""
from alembic import op

revision = "0021"
down_revision = "0020"


def upgrade() -> None:
    op.execute("ALTER TABLE ops.report ADD COLUMN year_only boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE ops.report DROP COLUMN year_only")
