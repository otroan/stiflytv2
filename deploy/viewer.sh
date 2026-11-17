#!/usr/bin/env bash
# The read-only map viewer and API, on 127.0.0.1:8100 only.
#   deploy/viewer.sh start|stop|restart|status
# Reach it from a laptop with:  ssh -N -L 8100:127.0.0.1:8100 <user>@<server>
# and open http://localhost:8100/
cd "$(dirname "$0")/.." || exit 1
mkdir -p var/log var/run
pidfile=var/run/viewer.pid

running() { [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; }

start() {
    if running; then echo "already running, pid $(cat "$pidfile")"; return; fi
    nohup .venv/bin/uvicorn stiflyt2.api.app:app --host 127.0.0.1 --port 8100 \
        >> var/log/viewer.log 2>&1 < /dev/null &
    echo $! > "$pidfile"
    echo "started, pid $!"
}

stop() {
    if running; then kill "$(cat "$pidfile")"; fi
    rm -f "$pidfile"
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    restart) stop; sleep 1; start ;;
    status) if running; then echo "running, pid $(cat "$pidfile")"; else echo "not running"; exit 1; fi ;;
    *) echo "usage: $0 start|stop|restart|status"; exit 2 ;;
esac
