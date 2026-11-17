"""ops: what happens to the routes, as opposed to what the routes are. First: signs.

The operations layer lies on top of the routes and the graph and never
changes them. What it keeps points at a junction, a place or a stretch in
`core` by id, and is read through `identity.current`, so that it holds when
junctions are joined or a stretch is divided.

Sign sites, posts and signs are worked out from the graph each time they are
asked for. What is kept here is what a person has decided about them: that
a site is accepted, where a post stands, which way an arrow points, another
text or distance; and the orders taken out, with what each sign said then.

Also: a third role. `operations` may do everything here and may not change
routes; `editor`, the route responsible, may do both.

Made on this track, outside the overlay. The schema is made by
`db/bootstrap.sql`. On a database from before this migration, run as the
database's owner first:

    CREATE SCHEMA IF NOT EXISTS ops AUTHORIZATION stiflyt2_owner;
    GRANT USAGE ON SCHEMA ops TO stiflyt2_api;
"""
from alembic import op

revision = "0008"
down_revision = "0007"

UP = r"""
ALTER TABLE access.person DROP CONSTRAINT person_role_check;
ALTER TABLE access.person ADD CONSTRAINT person_role_check CHECK (role IN ('reader', 'operations', 'editor'));

-- A sign site is a place, or a junction that is no place: one or more posts close together.
CREATE TABLE ops.sign_site (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    at_type         text NOT NULL CHECK (at_type IN ('place', 'node')),
    at_id           uuid NOT NULL,
    area            text NOT NULL,                  -- the area whose numbers its code is from
    code            text UNIQUE,                    -- BRE-SS-0001: given when the site is accepted
    status          text NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed', 'accepted', 'rejected', 'installed')),
    name            text,                           -- said by hand; else the place's name
    back_text       text,                           -- said by hand; else name and position
    send_to_name    text,
    send_to_address text,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text,
    UNIQUE (at_type, at_id)
);

-- A post stands at a junction. The first post of a junction is there without being kept: a row
-- says where a person has put it, that it is taken out, or that it is a further post.
CREATE TABLE ops.sign_post (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id     uuid NOT NULL,
    no          integer NOT NULL DEFAULT 1 CHECK (no >= 1),
    geom        geometry(Point, 25833),             -- NULL: where it is assumed to stand
    taken_out   boolean NOT NULL DEFAULT false,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text,
    UNIQUE (node_id, no)
);

-- What a person has said about one sign: found by the junction it is at, the stretch it points
-- along and where it points to. What is NULL is as worked out.
CREATE TABLE ops.sign (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id     uuid NOT NULL,
    first_edge  uuid NOT NULL,
    to_type     text NOT NULL CHECK (to_type IN ('place', 'node')),
    to_id       uuid NOT NULL,
    post_no     integer,
    direction   text CHECK (direction IN ('venstre', 'rett fram', 'høyre')),
    colour      text CHECK (colour IN ('trehvit', 'grønn')),
    text        text,
    distance_m  real CHECK (distance_m >= 0),
    taken_out   boolean NOT NULL DEFAULT false,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text,
    UNIQUE (node_id, first_edge, to_type, to_id)
);

-- An order as it was taken out: what each sign said then. Never changed.
CREATE TABLE ops.sign_order (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at      timestamptz NOT NULL DEFAULT now(),
    by      text NOT NULL,
    area    text NOT NULL,
    note    text,
    rows    jsonb NOT NULL
);

CREATE TABLE ops.event (
    seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    who         text NOT NULL,
    through     text NOT NULL DEFAULT 'viewer',
    subject     text NOT NULL,                      -- 'site', 'post', 'sign', 'order'
    subject_id  text NOT NULL,
    type        text NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX event_subject ON ops.event (subject, subject_id);

GRANT SELECT, INSERT, UPDATE ON ops.sign_site, ops.sign_post, ops.sign TO stiflyt2_api;
GRANT SELECT, INSERT ON ops.sign_order, ops.event TO stiflyt2_api;
"""

DOWN = r"""
DROP TABLE ops.event, ops.sign_order, ops.sign, ops.sign_post, ops.sign_site;
UPDATE access.person SET role = 'reader' WHERE role = 'operations';
ALTER TABLE access.person DROP CONSTRAINT person_role_check;
ALTER TABLE access.person ADD CONSTRAINT person_role_check CHECK (role IN ('reader', 'editor'));
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
