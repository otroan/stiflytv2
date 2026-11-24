"""walk.edge: which piece of the network a stretch is in, and how big the piece is

The network falls into one great piece and thousands of small ones: a path on
the plateau that joins nothing, a road on an island with no ferry. A point is
joined to the nearest stretch of a great piece rather than to a small one,
unless both ends of the leg lie on the same small piece. Filled in when the
network is built (`stiflyt2 planner build`).
"""
from alembic import op

revision = "0026"
down_revision = "0025"


def upgrade() -> None:
    op.execute("""ALTER TABLE walk.edge ADD COLUMN piece bigint, ADD COLUMN piece_nodes integer""")


def downgrade() -> None:
    op.execute("ALTER TABLE walk.edge DROP COLUMN piece, DROP COLUMN piece_nodes")
