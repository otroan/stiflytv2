"""A sign that was ordered is up

Between an order and the mountain there is a winter. A sign that has been
ordered is waiting until a person says it is up: `up_order` is the order it
was made in, and a sign ordered anew is waiting again.
"""
from alembic import op

revision = "0013"
down_revision = "0012"


def upgrade() -> None:
    op.execute("""ALTER TABLE ops.sign ADD COLUMN up_order bigint;
                  ALTER TABLE ops.sign_post ADD COLUMN name_up_order bigint""")


def downgrade() -> None:
    op.execute("""ALTER TABLE ops.sign DROP COLUMN up_order;
                  ALTER TABLE ops.sign_post DROP COLUMN name_up_order""")
