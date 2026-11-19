"""Features, roles as bundles of them, and what one person is given or denied

A role was one of three, each allowed what the one before it was. A role is
now a name for a bundle of features (`stiflyt2.features`), so that new roles
can be made without new code; a person may be given a feature their role does
not have, or have one taken away; and a feature as a whole can be off, for
those it is given to, or for everybody who is let in.

The three roles there were are made as bundles of what they could do, so
nobody's access changes.
"""
from alembic import op

revision = "0016"
down_revision = "0015"

UP = r"""
CREATE TABLE access.role (
    name      text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9_-]{1,39}$'),
    features  text[] NOT NULL DEFAULT '{}',
    about     text
);
INSERT INTO access.role (name, features, about) VALUES
    ('reader', '{}', 'Looks, and changes nothing'),
    ('operations', '{signs}', 'Decides about signs and takes out orders, and does not change the routes'),
    ('editor', '{routes,signs}', 'The route responsible: changes the routes, and may do what operations does');
ALTER TABLE access.person DROP CONSTRAINT person_role_check;
ALTER TABLE access.person ADD CONSTRAINT person_role_fkey FOREIGN KEY (role) REFERENCES access.role(name) ON UPDATE CASCADE;

CREATE TABLE access.feature (
    key    text PRIMARY KEY,
    state  text NOT NULL CHECK (state IN ('off', 'given', 'all')),
    by     text NOT NULL,
    at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE access.person_feature (
    email    text NOT NULL REFERENCES access.person(email) ON UPDATE CASCADE ON DELETE CASCADE,
    feature  text NOT NULL,
    given    boolean NOT NULL,          -- true: has it whatever the role says; false: has it not
    by       text NOT NULL,
    at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (email, feature)
);
GRANT SELECT ON access.role, access.feature, access.person_feature TO stiflyt2_api;
"""

DOWN = r"""
DROP TABLE access.person_feature, access.feature;
ALTER TABLE access.person DROP CONSTRAINT person_role_fkey;
UPDATE access.person SET role = 'reader' WHERE role NOT IN ('reader', 'operations', 'editor');
ALTER TABLE access.person ADD CONSTRAINT person_role_check CHECK (role IN ('reader', 'operations', 'editor'));
DROP TABLE access.role;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
