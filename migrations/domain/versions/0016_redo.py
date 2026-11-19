"""What was taken back can be brought back

Undo puts the rows of an action back as they were before it. To bring the
action back again (redo), the rows as they stood when it was taken back are
kept beside the rows as they were before it, with who took it back and how
they asked: redo is for the one who took it back.

An action taken back before this migration has nothing kept for it, and
cannot be brought back.
"""
from alembic import op

revision = "d0016"
down_revision = "d0015"


def upgrade() -> None:
    op.execute("""ALTER TABLE core.undo_log
                      ADD COLUMN new jsonb,            -- the row as it stood when the action was taken back
                      ADD COLUMN undone_by text,
                      ADD COLUMN undone_through text""")


def downgrade() -> None:
    op.execute("ALTER TABLE core.undo_log DROP COLUMN new, DROP COLUMN undone_by, DROP COLUMN undone_through")
