"""GPX tracks checked against the height model and Naismith's rule

`ops.track_line.checked` is what a track cleaned comes to when it is compared
with Kartverket's height model along the same way, every 10 m: the climb by
the device and by the model, how far apart their heights are, and the time
Naismith's rule gives against the time walking. Worked out from the line, so
it belongs with it: made again when the line is.
"""
from alembic import op

revision = "0030"
down_revision = "0029"


def upgrade() -> None:
    op.execute("""ALTER TABLE ops.track_line ADD COLUMN checked jsonb;
                  GRANT UPDATE (checked) ON ops.track_line TO stiflyt2_api""")


def downgrade() -> None:
    op.execute("ALTER TABLE ops.track_line DROP COLUMN checked")
