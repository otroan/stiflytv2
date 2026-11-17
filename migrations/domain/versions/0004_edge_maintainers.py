"""core.edge_revision.maintainers: who maintains a stretch, which can be several"""
from alembic import op

revision = "d0004"
down_revision = "d0003"


def upgrade() -> None:
    op.execute("ALTER TABLE core.edge_revision ADD COLUMN maintainers text[] NOT NULL DEFAULT '{}'")


def downgrade() -> None:
    op.execute("ALTER TABLE core.edge_revision DROP COLUMN maintainers")
