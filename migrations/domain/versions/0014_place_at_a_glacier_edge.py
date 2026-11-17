"""A place may be the edge of a glacier

Where a route steps onto the ice, or off it, is somewhere a sign points to
and a walk is planned to: "to the edge of the glacier". It is no hut and no
parking, and it is not just any other place either: whoever works out signs,
or what a route asks of the one who walks it, has to know that the ice
begins there. So it is a kind of place of its own.
"""
from alembic import op

revision = "d0014"
down_revision = "d0013"


def upgrade() -> None:
    op.execute("ALTER TABLE core.place DROP CONSTRAINT place_kind_check")
    op.execute("""ALTER TABLE core.place ADD CONSTRAINT place_kind_check
                  CHECK (kind IN ('hut', 'parking', 'glacier_edge', 'other'))""")


def downgrade() -> None:
    op.execute("UPDATE core.place SET kind = 'other' WHERE kind = 'glacier_edge'")
    op.execute("ALTER TABLE core.place DROP CONSTRAINT place_kind_check")
    op.execute("ALTER TABLE core.place ADD CONSTRAINT place_kind_check CHECK (kind IN ('hut', 'parking', 'other'))")
