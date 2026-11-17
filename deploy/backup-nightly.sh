#!/usr/bin/env bash
# Nightly copy of everything that is yours (core and history), and of who uses
# the tool and the feedback kept. Run from cron:
#   50 3 * * * $HOME/stiflytv2/deploy/backup-nightly.sh
# Copies go to var/backup; the newest 14 of each kind are kept. Appends to
# var/log/backup.log. A failure is kept with the feedback, and the page says so.
#
# Off the server too, when var/backup-repo is a git repository with a remote:
# the newest copy of each kind is committed there under one name and pushed, so
# that the repository's history is the copies night by night. The key to push
# with is named in var/backup-repo/.key (a path), and can do nothing else.
cd "$(dirname "$0")/.." || exit 1
mkdir -p var/log var/backup
exec >> var/log/backup.log 2>&1
echo "== $(date -u +%FT%TZ)"
out=$(.venv/bin/stiflyt2 overlay backup --to var/backup --keep 14 2>&1)
status=$?
echo "$out"
echo "exit $status"
if [ $status -ne 0 ]; then
    .venv/bin/stiflyt2 feedback failed "nightly copy" "$(echo "$out" | tail -3 | tr '\n' ' ')"
    exit $status
fi

repo=var/backup-repo
[ -d "$repo/.git" ] || exit 0
pushed=$(
    set -e
    for kind in overlay tool; do
        newest=$(ls -t var/backup/"$kind"-*.dump | head -1)
        cp "$newest" "$repo/$kind.dump"
    done
    [ -f "$repo/.key" ] && export GIT_SSH_COMMAND="ssh -i $(cat "$repo/.key") -o IdentitiesOnly=yes -o BatchMode=yes"
    git -C "$repo" add overlay.dump tool.dump
    git -C "$repo" diff --cached --quiet || git -C "$repo" -c user.name="stiflyt2 nightly copy" -c user.email="noreply@localhost" \
        commit -q -m "Copy of $(date -u +%FT%TZ)"
    git -C "$repo" push -q origin HEAD:main
    echo "pushed $(git -C "$repo" rev-parse --short HEAD)"
) 2>&1
status=$?
echo "$pushed"
echo "push exit $status"
if [ $status -ne 0 ]; then
    .venv/bin/stiflyt2 feedback failed "nightly copy off the server" "$(echo "$pushed" | tail -3 | tr '\n' ' ')"
fi
exit $status
