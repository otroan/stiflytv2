"""feedback.entry: what the tool refused, what failed, and what people say is missing

About the tool, not about the routes, so it lies outside `core` and `history`
and on this track: taking the overlay away leaves it standing.

The schema is made by `db/bootstrap.sql`. On a database from before this
migration, run as the database's owner first:

    CREATE SCHEMA IF NOT EXISTS feedback AUTHORIZATION stiflyt2_owner;
    GRANT USAGE ON SCHEMA feedback TO stiflyt2_api;
"""
from alembic import op

revision = "0005"
down_revision = "0004"

UP = r"""
CREATE TABLE feedback.entry (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    kind        text NOT NULL CHECK (kind IN ('refused', 'failed', 'reported')),
    through     text NOT NULL CHECK (through IN ('viewer', 'mcp', 'api')),
    who         text,
    what        text NOT NULL,      -- the request or tool: 'POST /api/v1/owned/routes/{route_id}/via', 'note'
    status      integer,            -- of the answer, for a request
    message     text NOT NULL,      -- the sentence the person was told, or wrote
    -- The message with ids and numbers taken out: entries that say the same are counted together.
    pattern     text GENERATED ALWAYS AS (regexp_replace(regexp_replace(message,
                    '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '{id}', 'g'),
                    '[0-9]+([.,][0-9]+)?', '#', 'g')) STORED,
    arguments   jsonb,              -- what was asked with
    about_type  text CHECK (about_type IN ('route', 'place', 'edge', 'node')),
    about_id    text,
    trace       text,
    seen_at     timestamptz,
    fixed_in    text,               -- the commit that put it right
    CHECK ((about_type IS NULL) = (about_id IS NULL))
);
CREATE INDEX entry_open ON feedback.entry (kind, what, pattern) WHERE seen_at IS NULL;

GRANT SELECT, INSERT ON feedback.entry TO stiflyt2_api;
GRANT UPDATE (seen_at, fixed_in) ON feedback.entry TO stiflyt2_api;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute("DROP TABLE feedback.entry")
