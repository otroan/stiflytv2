"""core.route.reviewed: routes made in bulk are owned but not yet looked at"""
from alembic import op

revision = "d0005"
down_revision = "d0004"


def upgrade() -> None:
    op.execute("ALTER TABLE core.route ADD COLUMN reviewed boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE core.route DROP COLUMN reviewed")
