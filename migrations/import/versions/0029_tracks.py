"""GPX tracks walked on the routes: the feature `tracks` (step 3 of the plan)

A track comes as a file (GPX), or as a line from v1, which kept the line
alone. Three tables, so that what is changed and taken back stays small:

- `ops.track_raw`: the track as it came, every point with its height (Z)
  and its time (M, seconds since 1970), kept once by the digest of what came.
  Never changed; it stays when a track is taken away, so that Undo can bring
  the track back. The file itself, where there is one, is on the server's
  disk (`settings.tracks_dir`).
- `ops.track`: what is said of it (its name, its area) and what was cut out
  of it by hand, as ranges of the points as they came. Changed by people,
  and taken back with Undo, as an issue is.
- `ops.track_line`: the track cleaned (jumps and points while standing
  still taken out, cut where the device was off, what was cut out by hand
  left out), with what it comes to: length, time walking, climb. Worked out
  from the two above, once for each way it has been cut (`key`): Undo puts
  the key back, and the line for it is still here.
"""
from alembic import op

revision = "0029"
down_revision = "0028"

UP = r"""
CREATE TABLE ops.track_raw (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sha256      text NOT NULL UNIQUE,                  -- of the file as it came, or of the line from v1
    ext         text CHECK (ext IN ('gpx')),           -- the file kept on disk; NULL: there is none (from v1)
    bytes       bigint,
    points      integer NOT NULL,
    has_time    boolean NOT NULL,                      -- whether M is the time of each point
    has_height  boolean NOT NULL,                      -- whether Z is the device's height of each point
    named       text,                                  -- the name the file gives it
    geom        geometry(MultiLineStringZM, 25833) NOT NULL,
    made_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ops.track (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_id      uuid NOT NULL REFERENCES ops.track_raw (id),
    name        text NOT NULL,
    area        text,
    origin      text NOT NULL DEFAULT 'upload',        -- or where it came from: 'v1:<id>'
    cuts        jsonb NOT NULL DEFAULT '[]',           -- cut out by hand: [{"from": i, "to": j}], points as they came
    line_key    text NOT NULL,                         -- which of its cleaned lines is the one now
    made_at     timestamptz NOT NULL DEFAULT now(),
    made_by     text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL
);
CREATE INDEX track_area ON ops.track (area);
CREATE INDEX track_raw_id ON ops.track (raw_id);

CREATE TABLE ops.track_line (
    track_id    uuid NOT NULL,
    key         text NOT NULL,                         -- how it was cleaned, and what was cut out by hand
    geom        geometry(MultiLineStringZM, 25833),    -- NULL: nothing is left of it
    length_m    double precision NOT NULL,
    parts       integer NOT NULL,
    points      integer NOT NULL,
    started_at  timestamptz,
    ended_at    timestamptz,
    total_s     double precision,                      -- from the first point to the last, parts and stops and all
    moving_s    double precision,                      -- walking: without the stops and the gaps between parts
    up_m        double precision,                      -- by the device's heights
    down_m      double precision,
    low_m       double precision,
    high_m      double precision,
    cleaned     jsonb NOT NULL,                        -- what was taken out, and the stops
    made_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (track_id, key)
);
CREATE INDEX track_line_geom ON ops.track_line USING gist (geom);

CREATE TRIGGER undo_log AFTER INSERT OR UPDATE OR DELETE ON ops.track
    FOR EACH ROW EXECUTE FUNCTION ops.log_for_undo();

GRANT SELECT, INSERT, UPDATE, DELETE ON ops.track TO stiflyt2_api;
GRANT SELECT, INSERT ON ops.track_raw TO stiflyt2_api;
GRANT SELECT, INSERT, DELETE ON ops.track_line TO stiflyt2_api;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute("DROP TABLE ops.track_line; DROP TABLE ops.track; DROP TABLE ops.track_raw")
