#!/usr/bin/env bash
# The browser check against a viewer on the test database, on the server the tests run on.
#
#   STIFLYT2_SSH=<user>@<server> tests/browser/run.sh
#
# Starts a viewer there on the test database (port 8101, local, no sign-in), makes the clean
# route candidates of one area yours in it, opens a tunnel, walks through the pages in Chrome on
# this machine (changing a route's name there to try undo and redo), and stops the viewer again. Run it after pytest, not during: pytest makes the test
# database anew. STIFLYT2_DIR is the checkout on the server (stiflytv2), AREA the area (bre).
set -u
: "${STIFLYT2_SSH:?name the server: STIFLYT2_SSH=<user>@<server>}"
dir=${STIFLYT2_DIR:-stiflytv2} area=${AREA:-bre} port=${PORT:-8113}
here=$(cd "$(dirname "$0")" && pwd)
ssh_() { ssh -o BatchMode=yes -o ConnectTimeout=20 "$STIFLYT2_SSH" "$@"; }

stop() {
    [ -n "${tunnel:-}" ] && kill "$tunnel" 2>/dev/null
    ssh_ 'bash -s' <<'REMOTE'
for pid in $(pgrep -u "$USER" -f "uvicorn stiflyt2.api.app:app --host 127.0.0.1 --port 8101"); do kill "$pid"; done
REMOTE
}
trap stop EXIT

ssh_ "DIR=$dir AREA=$area bash -s" <<'REMOTE' || exit 2
cd "$DIR" || exit 1
pgrep -u "$USER" -f pytest >/dev/null && { echo "pytest is running: wait for it"; exit 1; }
for pid in $(pgrep -u "$USER" -f "uvicorn stiflyt2.api.app:app --host 127.0.0.1 --port 8101"); do kill "$pid"; done
conn=$(grep '^STIFLYT2_TEST_ROLE_CONN=' .env | cut -d= -f2- | tr -d "\"'")
export STIFLYT2_API_DSN="dbname=stiflyt2_test user=stiflyt2_api $conn" STIFLYT2_EDITOR="Test Editor" STIFLYT2_GOOGLE_CLIENT_ID=""
# The features that are given to nobody of themselves are switched on for all, in the test database, to be tried.
.venv/bin/python -c "
import psycopg
with psycopg.connect(dbname='stiflyt2_test', autocommit=True) as c:
    c.execute('SET ROLE stiflyt2_owner')
    for key in ('trips', 'presence'):
        c.execute(\"INSERT INTO access.feature (key, state, by) VALUES (%s, 'all', 'the walk-through') ON CONFLICT (key) DO UPDATE SET state = 'all'\", (key,))
"
nohup .venv/bin/uvicorn stiflyt2.api.app:app --host 127.0.0.1 --port 8101 --app-dir src > /tmp/stiflyt2-test-viewer.log 2>&1 &
for _ in $(seq 20); do curl -s -o /dev/null http://127.0.0.1:8101/api/v1/status && break; sleep 0.5; done
curl -s -X POST "http://127.0.0.1:8101/api/v1/areas/$AREA/routes"; echo
REMOTE

ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -L "$port:127.0.0.1:8101" "$STIFLYT2_SSH" &
tunnel=$!
sleep 2
STIFLYT2_EDIT=1 node "$here/smoke.mjs" "http://localhost:$port/"
