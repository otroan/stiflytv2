"""A changeset is an item of work: what a person has changed, one way in, since they last saved

A changeset was one per person, opened by their first change and in practice
never closed. It now belongs to a person and the way they came in (the map, an
assistant, a script), and is what "Save" closes and "Discard" takes back: an
item of work, usually one route, sometimes two that are joined.

The changesets that stand open are closed here: what is in them was never
asked about, and is not to be offered for discarding as one item.
"""
from alembic import op

revision = "d0017"
down_revision = "d0016"


def upgrade() -> None:
    op.execute("ALTER TABLE core.changeset ADD COLUMN through text")
    op.execute("UPDATE core.changeset SET state = 'closed', closed_at = now() WHERE state = 'open'")


def downgrade() -> None:
    op.execute("ALTER TABLE core.changeset DROP COLUMN through")
