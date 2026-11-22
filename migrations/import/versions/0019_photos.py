"""Photos from the routes: the feature `photos` (step 3 of the plan)

A photo is kept once, by the SHA-256 digest of its file, in the photos
directory on the server's disk (`settings.photos_dir`), with a copy 1600 px
long to show and a thumbnail. Here is what is known of it: where it was taken
(from the camera's position, or placed by hand), when, which way the camera
looked, what is said of it, and the issue it shows, if any.

Where it was taken is its own position, as a camera has it: a photo is not
hung on the stretches. A route's photos are those taken near it.
"""
from alembic import op

revision = "0019"
down_revision = "0018"

UP = r"""
CREATE TABLE ops.photo (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sha256      text NOT NULL UNIQUE,              -- of the file as it came: where it is kept, and that it is kept once
    ext         text NOT NULL CHECK (ext IN ('jpg', 'png', 'heic', 'webp')),
    bytes       bigint NOT NULL,
    width       integer,                           -- as it is shown, turned upright
    height      integer,
    taken_at    timestamp,                         -- the camera's own clock, as it says it
    geom        geometry(Point, 25833),            -- where it was taken; NULL: not placed yet
    placed      text CHECK (placed IN ('camera', 'hand')),
    facing      real CHECK (facing >= 0 AND facing < 360),     -- which way the camera looked, where it says
    area        text,
    caption     text,
    entry_id    uuid,                              -- the issue it shows (ops.entry)
    origin      text NOT NULL DEFAULT 'upload',    -- or where it came from: 'v1:<file>'
    made_at     timestamptz NOT NULL DEFAULT now(),
    made_by     text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL,
    CHECK ((geom IS NULL) = (placed IS NULL))
);
CREATE INDEX photo_geom ON ops.photo USING gist (geom);
CREATE INDEX photo_area ON ops.photo (area);
CREATE INDEX photo_entry ON ops.photo (entry_id);

CREATE TRIGGER undo_log AFTER INSERT OR UPDATE OR DELETE ON ops.photo
    FOR EACH ROW EXECUTE FUNCTION ops.log_for_undo();

GRANT SELECT, INSERT, UPDATE, DELETE ON ops.photo TO stiflyt2_api;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute("DROP TABLE ops.photo")
