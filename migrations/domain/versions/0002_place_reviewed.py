"""core.place.reviewed: places made by default are owned but not yet looked at"""
from alembic import op

revision = "d0002"
down_revision = "d0001"


def upgrade() -> None:
    op.execute("ALTER TABLE core.place ADD COLUMN reviewed boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE core.place DROP COLUMN reviewed")
