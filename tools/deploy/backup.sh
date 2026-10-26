#!/usr/bin/env bash
# workmcp daily database backup (design M5 remote 9.5, approval step E).
#
# Runs as the workmcp user (see workmcp-backup.service / backup.timer). It copies the database with
# SQLite's online backup API (safe while the server runs, WAL-aware), checks the copy, and keeps the
# newest 7 copies. The copies stay INSIDE the data home on this VPS: nothing is copied elsewhere
# (project rule: the data lives in two places only). Whether a Hostinger snapshot may hold a copy
# is a separate decision of the owner (design Q7); this script never does it.
#
# The output names files and sizes only, never database content.
#
# Environment (defaults match the unit):
#   WORKMCP_HOME  data home, default /home/workmcp/Motive_work
#   WORKMCP_PY    python of the venv, default /home/workmcp/app/.venv/bin/python
#   WORKMCP_KEEP  number of copies to keep, default 7

set -u
umask 077

HOME_DIR="${WORKMCP_HOME:-/home/workmcp/Motive_work}"
PY="${WORKMCP_PY:-/home/workmcp/app/.venv/bin/python}"
KEEP="${WORKMCP_KEEP:-7}"
DB="$HOME_DIR/db/work.sqlite3"
DEST="$HOME_DIR/backup"
STAMP="$(date +%Y%m%d)"
TARGET="$DEST/work-$STAMP.sqlite3"

if [ ! -f "$DB" ]; then
    echo "backup: database not found: $DB" >&2
    exit 2
fi
mkdir -p "$DEST" || exit 2

# One copy per day: a second run the same day replaces the day's copy through a temporary file.
TMP="$TARGET.partial"
rm -f "$TMP"

"$PY" -I - "$DB" "$TMP" <<'PY'
import sqlite3
import sys

source_path, target_path = sys.argv[1], sys.argv[2]
source = sqlite3.connect(f'file:{source_path}?mode=ro', uri=True, timeout=30)
target = sqlite3.connect(target_path)
try:
    source.backup(target)
    status = target.execute('PRAGMA integrity_check').fetchone()[0]
finally:
    target.close()
    source.close()
if status != 'ok':
    print(f'backup: integrity_check failed: {status}', file=sys.stderr)
    sys.exit(3)
PY
rc=$?
if [ "$rc" -ne 0 ]; then
    rm -f "$TMP"
    echo "backup: failed (exit $rc)" >&2
    exit "$rc"
fi

mv -f "$TMP" "$TARGET" || exit 2

# Keep the newest $KEEP copies (names sort by date).
ls -1 "$DEST"/work-*.sqlite3 2>/dev/null | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do
    rm -f -- "$old"
done

echo "backup: ok $(basename "$TARGET") $(stat -c %s "$TARGET") bytes; kept $(ls -1 "$DEST"/work-*.sqlite3 | wc -l)"
