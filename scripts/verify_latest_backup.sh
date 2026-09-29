#!/usr/bin/env bash
# Verify the most recent backup actually restores.
#
# Run by a weekly timer. This is the step that turns "we take backups" into "we
# know our backups work" — an untested backup is a hypothesis, and the failures
# it hides (truncated archive, table dropped by a migration, incompatible
# pg_dump version) are only visible on the day you need it.
#
# Usage:
#   bash scripts/verify_latest_backup.sh [n]
#
# ``n`` selects the nth-newest backup (default 1 = the latest). Use 2 to check
# that yesterday's archive is still readable, which also catches a retention
# job that pruned too aggressively.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_ROOT="${BACKUP_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)/backups}"
N="${1:-1}"

# ``ls -1d`` on a glob sorted in reverse gives newest first. Directories only,
# so a stray file in backups/ cannot be mistaken for a backup.
mapfile -t DIRS < <(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort -r)

if [ "${#DIRS[@]}" -eq 0 ]; then
    echo "没有找到任何备份（$BACKUP_ROOT）；先运行 scripts/backup.sh" >&2
    exit 1
fi
if [ "${#DIRS[@]}" -lt "$N" ]; then
    echo "只找到 ${#DIRS[@]} 个备份，无法取第 $N 新的一个" >&2
    exit 1
fi

TARGET="${DIRS[$((N - 1))]}"
echo "==> 校验第 $N 新的备份: $TARGET"
exec bash "$SCRIPT_DIR/restore_backup.sh" --verify "$TARGET"
