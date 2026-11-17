"""import_control.pruned_artifact: snapshots whose kept original file was removed by retention"""
from alembic import op

revision = "0003"
down_revision = "0002"

UP = r"""
CREATE TABLE import_control.pruned_artifact (
    snapshot_id  uuid PRIMARY KEY,
    path         text NOT NULL,
    pruned_at    timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT ON import_control.pruned_artifact TO stiflyt2_import;
GRANT SELECT ON import_control.pruned_artifact TO stiflyt2_api;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute("DROP TABLE import_control.pruned_artifact")
