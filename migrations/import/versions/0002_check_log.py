"""import_control.check_log: one row per refresh check of a source and scope"""
from alembic import op

revision = "0002"
down_revision = "0001"

UP = r"""
CREATE TABLE import_control.check_log (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_key   text NOT NULL,
    scope        text NOT NULL,
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz NOT NULL DEFAULT now(),
    outcome      text NOT NULL CHECK (outcome IN ('unchanged', 'imported', 'rejected', 'error')),
    snapshot_id  uuid,
    detail       text NOT NULL DEFAULT ''
);
CREATE INDEX check_log_latest ON import_control.check_log (source_key, scope, finished_at DESC);

GRANT SELECT, INSERT ON import_control.check_log TO stiflyt2_import;
GRANT SELECT ON import_control.check_log TO stiflyt2_api;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute("DROP TABLE import_control.check_log")
