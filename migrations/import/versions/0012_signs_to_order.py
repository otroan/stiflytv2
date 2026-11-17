"""A sign is marked to be made, and an order is the signs that are marked

Most signs hang where they are to hang already. An order is seldom a whole
site: it is the signs that are to be replaced, one here and two there, worn
or broken or saying something that is no longer so; and now and then the
signs of a site that is new. So a person marks a sign to be made, with why,
and an order takes the signs that are marked.
"""
from alembic import op

revision = "0012"
down_revision = "0011"


def upgrade() -> None:
    op.execute("""ALTER TABLE ops.sign ADD COLUMN to_order boolean NOT NULL DEFAULT false, ADD COLUMN why text;
                  ALTER TABLE ops.sign_post ADD COLUMN name_to_order boolean NOT NULL DEFAULT false""")


def downgrade() -> None:
    op.execute("""ALTER TABLE ops.sign DROP COLUMN to_order, DROP COLUMN why;
                  ALTER TABLE ops.sign_post DROP COLUMN name_to_order""")
