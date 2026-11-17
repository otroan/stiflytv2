"""access: who may use the tool, as what, and the tokens an assistant acts with

A person is known by the e-mail address they sign in with (Google), has the
name their changes are recorded under, and a role: a reader looks, an editor
(the route responsible) changes. A token lets a program act as a person
without signing in: an assistant over MCP, or a script. What comes in with a
token is recorded as that person's, and as having come that way.

The API reads these tables and marks when someone was last seen. It cannot
add or change a person or a token: that is done from the command line, as
the database's owner.

Also: a nightly job that fails is kept with the rest of the feedback, as
having come from a job.

The schema is made by `db/bootstrap.sql`. On a database from before this
migration, run as the database's owner first:

    CREATE SCHEMA IF NOT EXISTS access AUTHORIZATION stiflyt2_owner;
    GRANT USAGE ON SCHEMA access TO stiflyt2_api;
"""
from alembic import op

revision = "0006"
down_revision = "0005"

UP = r"""
CREATE TABLE access.person (
    email         text PRIMARY KEY CHECK (email = lower(email) AND email LIKE '%_@_%'),
    name          text NOT NULL UNIQUE CHECK (name <> ''),
    role          text NOT NULL CHECK (role IN ('reader', 'editor')),
    added_at      timestamptz NOT NULL DEFAULT now(),
    added_by      text NOT NULL,
    last_seen_at  timestamptz
);

CREATE TABLE access.token (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    digest        text NOT NULL UNIQUE,     -- sha256 of the token; the token itself is shown once and not kept
    email         text NOT NULL REFERENCES access.person(email) ON UPDATE CASCADE ON DELETE CASCADE,
    through       text NOT NULL CHECK (through IN ('mcp', 'api')),
    label         text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz
);

GRANT SELECT ON access.person, access.token TO stiflyt2_api;
GRANT UPDATE (last_seen_at) ON access.person TO stiflyt2_api;
GRANT UPDATE (last_used_at) ON access.token TO stiflyt2_api;

ALTER TABLE feedback.entry DROP CONSTRAINT entry_through_check;
ALTER TABLE feedback.entry ADD CONSTRAINT entry_through_check CHECK (through IN ('viewer', 'mcp', 'api', 'job'));
"""

DOWN = r"""
DELETE FROM feedback.entry WHERE through = 'job';
ALTER TABLE feedback.entry DROP CONSTRAINT entry_through_check;
ALTER TABLE feedback.entry ADD CONSTRAINT entry_through_check CHECK (through IN ('viewer', 'mcp', 'api'));
DROP TABLE access.token, access.person;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
