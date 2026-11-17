"""core.route and route_leg; marked and signed on every edge revision (ADR-003)"""
from alembic import op

revision = "d0003"
down_revision = "d0002"

UP = r"""
-- Whether a stretch is marked on the ground and whether it is signposted are
-- facts about the stretch, not the route. A glacier leg is signed but never marked.
ALTER TABLE core.edge_revision ADD COLUMN marked boolean, ADD COLUMN signed boolean;

-- A route is ours: its own id, a code unique in the overlay, an ordered list of legs.
-- code_provisional: the code is a temporary handle (such as a part label, jot91/1)
-- and has not been assigned by whoever comes to govern route codes.
CREATE TABLE core.route (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code              text NOT NULL,
    code_provisional  boolean NOT NULL DEFAULT false,
    name              text,            -- NULL: named after the places at its ends
    status            text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    version           integer NOT NULL DEFAULT 1,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX route_code ON core.route (lower(code)) WHERE status = 'active';

CREATE TABLE core.route_leg (
    route_id  uuid NOT NULL REFERENCES core.route(id),
    seq       integer NOT NULL,
    edge_id   uuid NOT NULL REFERENCES core.edge(id),
    reversed  boolean NOT NULL DEFAULT false,   -- walked from the edge's b node to its a node
    PRIMARY KEY (route_id, seq)
);
CREATE INDEX route_leg_edge ON core.route_leg (edge_id);

ALTER TABLE core.source_reference DROP CONSTRAINT source_reference_subject_type_check;
ALTER TABLE core.source_reference ADD CONSTRAINT source_reference_subject_type_check
    CHECK (subject_type IN ('place', 'node', 'edge', 'route'));
CREATE INDEX source_reference_external ON core.source_reference (source_key, external_id);

GRANT SELECT, INSERT, UPDATE ON core.route TO stiflyt2_api;
GRANT SELECT, INSERT, DELETE ON core.route_leg TO stiflyt2_api;
"""

DOWN = r"""
DROP TABLE core.route_leg, core.route;
DROP INDEX core.source_reference_external;
ALTER TABLE core.edge_revision DROP COLUMN marked, DROP COLUMN signed;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
