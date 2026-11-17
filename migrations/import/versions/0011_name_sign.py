"""A sign with the place's name on top of a post

Where nothing else says where one is (no hut, no car park), the post carries
a sign with the name of the place and no arrow: Viva. It is the exception,
so a person says which posts have one.
"""
from alembic import op

revision = "0011"
down_revision = "0010"


def upgrade() -> None:
    op.execute("""ALTER TABLE ops.sign_post ADD COLUMN name_sign boolean NOT NULL DEFAULT false,
                                            ADD COLUMN name_text text""")


def downgrade() -> None:
    op.execute("ALTER TABLE ops.sign_post DROP COLUMN name_sign, DROP COLUMN name_text")
