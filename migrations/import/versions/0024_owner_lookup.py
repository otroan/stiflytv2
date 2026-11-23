"""Who asked Kartverket's property register who owns what (the feature `landowners`)

Owners' names and addresses are personal data, and are not kept: they are
asked for when a person wants them. That the register was asked is kept, by
whom, when, and of which property units (the register's ids for them), so that
the use of DNT's access can be accounted for.
"""
from alembic import op

revision = "0024"
down_revision = "0023"

UP = r"""
CREATE TABLE ops.owner_lookup (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at       timestamptz NOT NULL DEFAULT now(),
    who      text NOT NULL,                 -- the name it was asked under
    email    text,                          -- and the address signed in with, where there is one
    through  text,                          -- viewer, api, mcp
    what     text NOT NULL,                 -- a route (its code) or a spot
    units    text[] NOT NULL                -- the register's ids of the units asked about
);
CREATE INDEX owner_lookup_at ON ops.owner_lookup (at);
GRANT SELECT, INSERT ON ops.owner_lookup TO stiflyt2_api;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute("DROP TABLE ops.owner_lookup")
