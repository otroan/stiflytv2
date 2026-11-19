"""Where each person last saved, in operations

What a person has decided about signs since they last saved is their item of
work, usually one sign site: saved, or discarded as a whole. Operations has
no changesets, so where they last saved is a mark of its own: the transaction
it was said in. Everything decided before this migration counts as saved.
"""
from alembic import op

revision = "0015"
down_revision = "0014"

UP = """
CREATE TABLE ops.saved (
    who      text NOT NULL,
    through  text NOT NULL DEFAULT 'viewer',
    txid     bigint NOT NULL,
    at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (who, through)
);
GRANT SELECT, INSERT, UPDATE ON ops.saved TO stiflyt2_api;
INSERT INTO ops.saved (who, through, txid)
SELECT who, through, max(txid) FROM ops.event WHERE txid IS NOT NULL GROUP BY who, through;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute("DROP TABLE ops.saved")
