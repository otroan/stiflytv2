#!/usr/bin/env bash
# Nightly check of every imported source, run from cron:
#   0 7 * * * $HOME/stiflytv2/deploy/refresh-nightly.sh
# After 06:30 UTC: Kartverket makes the Turrutebasen file between 04:40 and 06:25,
# and a run before that is a day behind.
# Appends to var/log/refresh.log. Exits with the status of `stiflyt2 refresh`:
# 1 when a delivery was refused or a source could not be checked.
# Last, what the page would warn of is sent, where a way to send is set up (`stiflyt2 health --alert`).
cd "$(dirname "$0")/.." || exit 1
mkdir -p var/log
exec >> var/log/refresh.log 2>&1
echo "== $(date -u +%FT%TZ)"
trap '.venv/bin/stiflyt2 health --alert' EXIT
# A run still going from the night before is left to finish.
flock -n var/log/refresh.lock .venv/bin/stiflyt2 refresh
status=$?
echo "exit $status"
# A night that went wrong is kept with the feedback, and the page says so to whoever works in it.
# (A delivery refused or a source that could not be checked is also in the check log, per source.)
if [ $status -gt 1 ]; then
    .venv/bin/stiflyt2 feedback failed "nightly check of the sources" "stiflyt2 refresh ended with status $status: see var/log/refresh.log"
fi
# The route planner's network, when a new extract of OpenStreetMap has come (weekly): some 11 minutes.
.venv/bin/stiflyt2 planner build --if-new || .venv/bin/stiflyt2 feedback failed "the planner's network" "stiflyt2 planner build did not finish: see var/log/refresh.log"
# GPX tracks added during the day, checked against Kartverket's height model and Naismith's rule.
.venv/bin/stiflyt2 tracks check || .venv/bin/stiflyt2 feedback failed "the tracks' check" "stiflyt2 tracks check did not finish: see var/log/refresh.log"
# Stretches that are new or were moved during the day get their heights. Asked once per line.
.venv/bin/stiflyt2 height fill || .venv/bin/stiflyt2 feedback failed "nightly heights" "stiflyt2 height fill did not get every stretch: see var/log/refresh.log"
exit $status
